#!/usr/bin/env python3
"""
OCI Cost Reports Web Dashboard
Flask app to visualize OCI cost data from Usage API.
"""

import csv
import io
import os
import time as _time
from datetime import datetime, timedelta

import oci
import pandas as pd
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

_cache = {}


def get_config():
    config = oci.config.from_file(
        file_location="E:\\oci-cost-reports\\oci_config",
        profile_name="DEFAULT"
    )
    oci.config.validate_config(config)
    return config


def _safe_float(val):
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def fetch_cost_data(days=30, force=False):
    now = datetime.utcnow()
    cache_key = f"days_{days}"

    if (
        not force
        and cache_key in _cache
        and _cache[cache_key]["timestamp"] is not None
        and (now - _cache[cache_key]["timestamp"]).seconds < 3600
    ):
        return _cache[cache_key]["data"]

    config = get_config()
    client = oci.usage_api.UsageapiClient(config)
    tenancy = config["tenancy"]

    end_date = now
    start_date = end_date - timedelta(days=days)
    time_start = start_date.strftime("%Y-%m-%dT00:00:00.000Z")
    time_end = end_date.strftime("%Y-%m-%dT00:00:00.000Z")

    def _query(granularity, group_by):
        req = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=tenancy,
            time_usage_started=time_start,
            time_usage_ended=time_end,
            granularity=granularity,
            group_by=group_by,
            compartment_depth=5,
        )
        return client.request_summarized_usages(req)

    # Query 1: daily by service + compartment
    resp1 = _query("DAILY", ["service", "compartmentName"])
    _time.sleep(1)

    # Query 2: monthly by resource
    resp2 = None
    try:
        resp2 = _query("MONTHLY", ["service", "compartmentName", "resourceId"])
    except Exception:
        pass
    _time.sleep(1)

    # Query 3: monthly by service for monthly comparison
    resp3 = None
    try:
        resp3 = _query("MONTHLY", ["service"])
    except Exception:
        pass

    # Process query 1
    rows = []
    for item in resp1.data.items:
        rows.append({
            "service": item.service,
            "compartment_name": item.compartment_name,
            "time_usage_started": str(item.time_usage_started).split("+")[0],
            "time_usage_ended": str(item.time_usage_ended).split("+")[0],
            "computed_amount": _safe_float(item.computed_amount),
            "computed_quantity": _safe_float(item.computed_quantity),
            "currency": item.currency,
        })

    # Process query 2 - resources
    resource_rows = []
    if resp2:
        for item in resp2.data.items:
            cost = _safe_float(item.computed_amount)
            if cost > 0:
                resource_rows.append({
                    "service": item.service,
                    "compartment_name": item.compartment_name,
                    "resource_id": item.resource_id or "N/A",
                    "resource_name": getattr(item, "resource_name", None) or item.resource_id or "N/A",
                    "computed_amount": cost,
                    "computed_quantity": _safe_float(item.computed_quantity),
                })

    # Process query 3 - monthly by service
    monthly_service = []
    if resp3:
        for item in resp3.data.items:
            monthly_service.append({
                "month": str(item.time_usage_started)[:7],
                "service": item.service,
                "cost": _safe_float(item.computed_amount),
            })

    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["service", "compartment_name", "time_usage_started", "computed_amount"])
    df_res = pd.DataFrame(resource_rows) if resource_rows else pd.DataFrame()

    # Top resources grouped (limit to 15 for speed)
    top_resources = []
    if not df_res.empty:
        res_grp = (
            df_res.groupby(["service", "compartment_name", "resource_name", "resource_id"])
            .agg(total_cost=("computed_amount", "sum"), total_qty=("computed_quantity", "sum"))
            .reset_index()
            .sort_values("total_cost", ascending=False)
            .head(15)
        )
        top_resources = res_grp.to_dict("records")
        for r in top_resources:
            r["total_cost"] = round(r["total_cost"], 2)
            r["total_qty"] = round(r["total_qty"], 2)

    # Enrich top 15 resources with names and tags
    resource_ids = [r["resource_id"] for r in top_resources if r["resource_id"] != "N/A"]
    resource_details = _fetch_resource_details(config, resource_ids)
    for r in top_resources:
        rid = r["resource_id"]
        if rid in resource_details:
            r["resource_name"] = resource_details[rid].get("name", r["resource_name"])
            r["created_by"] = resource_details[rid].get("created_by", "")
            r["created_on"] = resource_details[rid].get("created_on", "")

    # Monthly comparison
    monthly_totals = {}
    if monthly_service:
        for item in monthly_service:
            m = item["month"]
            monthly_totals[m] = monthly_totals.get(m, 0) + item["cost"]
    monthly_comparison = [{"month": k, "cost": round(v, 2)} for k, v in sorted(monthly_totals.items())]

    # Cost forecast
    daily_data = _daily_sum(df)
    forecast = []
    if len(daily_data) >= 7:
        recent_avg = sum(d["cost"] for d in daily_data[-7:]) / 7
        days_in_period = (end_date - start_date).days
        forecast_cost = round(recent_avg * days_in_period, 2)
        forecast = {
            "daily_avg": round(recent_avg, 2),
            "projected_total": forecast_cost,
            "days_in_period": days_in_period,
        }

    # Per-compartment resource breakdown
    compartment_resources = {}
    if not df_res.empty:
        for _, row in df_res.iterrows():
            comp = row["compartment_name"]
            if comp not in compartment_resources:
                compartment_resources[comp] = []
            compartment_resources[comp].append({
                "service": row["service"],
                "resource_name": row["resource_name"],
                "resource_id": row["resource_id"],
                "cost": round(row["computed_amount"], 2),
                "qty": round(row["computed_quantity"], 2),
            })
    # Sort each compartment's resources by cost desc
    for comp in compartment_resources:
        compartment_resources[comp].sort(key=lambda x: -x["cost"])
        compartment_resources[comp] = compartment_resources[comp][:30]

    result = {
        "summary": {
            "total_cost": round(df["computed_amount"].sum(), 2) if not df.empty else 0,
            "total_records": len(df),
            "services": int(df["service"].nunique()) if not df.empty else 0,
            "compartments": int(df["compartment_name"].nunique()) if not df.empty else 0,
            "period_start": start_date.strftime("%Y-%m-%d"),
            "period_end": end_date.strftime("%Y-%m-%d"),
        },
        "by_service": _group_sum(df, "service"),
        "by_compartment": _group_sum(df, "compartment_name"),
        "daily": daily_data,
        "by_service_compartment": _service_compartment_matrix(df),
        "top_resources": top_resources,
        "monthly_comparison": monthly_comparison,
        "forecast": forecast,
        "compartment_resources": compartment_resources,
        "raw_data": rows,
    }

    _cache[cache_key] = {"data": result, "timestamp": now}
    return result


def _fetch_resource_details(config, resource_ids):
    """Fetch resource names and tags using Resource Search API."""
    if not resource_ids:
        return {}

    search_client = oci.resource_search.ResourceSearchClient(config)
    details = {}

    for rid in resource_ids:
        if rid in details:
            continue
        try:
            resp = search_client.search_resources(
                search_details=oci.resource_search.models.FreeTextSearchDetails(
                    text=rid,
                ),
            )
            for result in resp.data.items:
                if result.identifier == rid:
                    created_by = ""
                    created_on = ""
                    if result.defined_tags:
                        for namespace, tags in result.defined_tags.items():
                            if isinstance(tags, dict):
                                for key, val in tags.items():
                                    if key.lower() == "createdby":
                                        created_by = val
                                    if key.lower() == "createdon":
                                        created_on = str(val)[:10] if val else ""
                    details[rid] = {
                        "name": result.display_name or rid,
                        "created_by": created_by,
                        "created_on": created_on,
                    }
                    break
            if rid not in details:
                details[rid] = {"name": rid, "created_by": "", "created_on": ""}
        except Exception:
            details[rid] = {"name": rid, "created_by": "", "created_on": ""}
        _time.sleep(0.1)

    return details


def _group_sum(df, col):
    if df.empty or col not in df.columns:
        return []
    grouped = df.groupby(col)["computed_amount"].sum().sort_values(ascending=False)
    return [{"name": k, "cost": round(v, 2)} for k, v in grouped.items()]


def _daily_sum(df):
    if df.empty or "time_usage_started" not in df.columns:
        return []
    df = df.copy()
    df["date"] = df["time_usage_started"].str[:10]
    daily = df.groupby("date")["computed_amount"].sum().sort_index()
    return [{"date": k, "cost": round(v, 2)} for k, v in daily.items()]


def _service_compartment_matrix(df):
    matrix = df.groupby(["service", "compartment_name"])["computed_amount"].sum().reset_index()
    matrix = matrix.sort_values("computed_amount", ascending=False)
    return matrix.head(30).to_dict("records")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    days = int(request.args.get("days", 30))
    force = request.args.get("force", "false").lower() == "true"
    days = min(max(days, 1), 365)
    data = fetch_cost_data(days=days, force=force)
    return jsonify(data)


@app.route("/api/export/csv")
def export_csv():
    days = int(request.args.get("days", 30))
    days = min(max(days, 1), 365)
    data = fetch_cost_data(days=days, force=False)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Service", "Compartment", "Cost (USD)"])
    for row in data["raw_data"]:
        writer.writerow([
            row["time_usage_started"][:10],
            row["service"],
            row["compartment_name"],
            row["computed_amount"],
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=oci_cost_report.csv"},
    )


if __name__ == "__main__":
    print("=" * 60)
    print("OCI Cost Dashboard")
    print("Open http://localhost:5000 in your browser")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
