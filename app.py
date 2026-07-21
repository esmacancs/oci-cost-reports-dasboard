#!/usr/bin/env python3
"""
OCI Cost Reports Web Dashboard
Flask app to visualize OCI cost data from Usage API.
"""

import csv
import hashlib
import io
import os
import time as _time
from datetime import datetime, timedelta
from functools import wraps

import oci
import pandas as pd
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.urandom(32).hex()

USERS = {
    "admin": {
        "password": hashlib.sha256("admin123".encode()).hexdigest(),
        "role": "admin",
        "display_name": "Administrator",
    },
    "viewer": {
        "password": hashlib.sha256("viewer123".encode()).hexdigest(),
        "role": "user",
        "display_name": "Viewer",
    },
}

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


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return jsonify({"success": False, "message": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        hashed = hashlib.sha256(password.encode()).hexdigest()
        user = USERS.get(username)
        if user and user["password"] == hashed:
            session["user"] = username
            session["role"] = user["role"]
            session["display_name"] = user["display_name"]
            return redirect(url_for("index"))
        error = "Invalid username or password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/user")
def api_user():
    if "user" not in session:
        return jsonify({"authenticated": False})
    return jsonify({
        "authenticated": True,
        "username": session["user"],
        "role": session["role"],
        "display_name": session["display_name"],
    })


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
@login_required
def index():
    return render_template("index.html", user_role=session.get("role", "user"), display_name=session.get("display_name", "User"))


@app.route("/api/data")
@login_required
def api_data():
    days = int(request.args.get("days", 30))
    force = request.args.get("force", "false").lower() == "true"
    days = min(max(days, 1), 365)
    data = fetch_cost_data(days=days, force=force)
    return jsonify(data)


@app.route("/api/export/csv")
@login_required
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


def _detect_resource_type(ocid):
    """Detect OCI resource type from OCID prefix."""
    if not ocid or ocid == "N/A":
        return None
    type_map = {
        "instance": "compute",
        "volume": "block_storage",
        "bootvolumebackup": "block_storage",
        "vcn": "network",
        "subnetwork": "network",
        "securitylist": "network",
        "loadbalancer": "load_balancer",
        "networkloadbalancer": "network_load_balancer",
        "networkfirewall": "network_firewall",
        "postgresqldbsystem": "postgresql",
        "containerengine": "oke",
        "dbcs": "database",
        "database": "database",
        "autonomousdatabase": "database",
        "mounttarget": "file_storage",
        "bucket": "object_storage",
        "digitalink": "digital_assistant",
        "analytics": "analytics",
        "integration": "integration",
        "streaming": "streaming",
    }
    ocid_lower = ocid.lower()
    for prefix, rtype in type_map.items():
        if f"ocid1.{prefix}." in ocid_lower:
            return rtype
    return None


def _resource_action(resource_id, action):
    """Execute start/stop/delete on an OCI resource."""
    config = get_config()
    rtype = _detect_resource_type(resource_id)

    if not rtype:
        return False, f"Cannot detect resource type from OCID"

    compartment_id = None
    # Extract compartment from OCID (3rd segment after ocid1.<type>.<region>.<compartment>)
    parts = resource_id.split(".")
    if len(parts) >= 4:
        # The compartment hash is embedded, we need to find it via API
        pass

    try:
        if rtype == "compute":
            client = oci.core.ComputeClient(config)
            if action == "start":
                resp = client.instance_action(resource_id, "START")
                return True, f"Instance starting..."
            elif action == "stop":
                resp = client.instance_action(resource_id, "STOP")
                return True, f"Instance stopping..."
            elif action == "delete":
                resp = client.delete_instance(resource_id)
                return True, f"Instance delete initiated..."

        elif rtype == "postgresql":
            if action == "delete":
                # Need compartment_id for PostgreSQL
                search_client = oci.resource_search.ResourceSearchClient(config)
                sr = search_client.search_resources(
                    search_details=oci.resource_search.models.FreeTextSearchDetails(text=resource_id)
                )
                if sr.data.items:
                    comp_id = sr.data.items[0].compartment_id
                    pg_client = oci.pdsql.PostgresqlClient(config)
                    resp = pg_client.delete_db_system(comp_id, resource_id)
                    return True, f"PostgreSQL delete initiated..."
                return False, "Could not find compartment for PostgreSQL resource"
            else:
                return False, "PostgreSQL does not support start/stop"

        elif rtype == "network_firewall":
            if action == "delete":
                search_client = oci.resource_search.ResourceSearchClient(config)
                sr = search_client.search_resources(
                    search_details=oci.resource_search.models.FreeTextSearchDetails(text=resource_id)
                )
                if sr.data.items:
                    comp_id = sr.data.items[0].compartment_id
                    fw_client = oci.network_firewall.NetworkFirewallClient(config)
                    resp = fw_client.delete_network_firewall(comp_id, resource_id)
                    return True, f"Network Firewall delete initiated..."
                return False, "Could not find compartment"
            else:
                return False, "Network Firewall does not support start/stop"

        elif rtype == "load_balancer":
            if action == "delete":
                search_client = oci.resource_search.ResourceSearchClient(config)
                sr = search_client.search_resources(
                    search_details=oci.resource_search.models.FreeTextSearchDetails(text=resource_id)
                )
                if sr.data.items:
                    comp_id = sr.data.items[0].compartment_id
                    lb_client = oci.load_balancer.LoadBalancerClient(config)
                    resp = lb_client.delete_load_balancer(resource_id)
                    return True, f"Load Balancer delete initiated..."
                return False, "Could not find compartment"
            else:
                return False, "Load Balancer does not support start/stop"

        elif rtype == "network_load_balancer":
            if action == "delete":
                search_client = oci.resource_search.ResourceSearchClient(config)
                sr = search_client.search_resources(
                    search_details=oci.resource_search.models.FreeTextSearchDetails(text=resource_id)
                )
                if sr.data.items:
                    comp_id = sr.data.items[0].compartment_id
                    nlb_client = oci.network_load_balancer.NetworkLoadBalancerClient(config)
                    resp = nlb_client.delete_network_load_balancer(resource_id)
                    return True, f"Network Load Balancer delete initiated..."
                return False, "Could not find compartment"
            else:
                return False, "Network Load Balancer does not support start/stop"

        elif rtype == "block_storage":
            if action == "delete":
                search_client = oci.resource_search.ResourceSearchClient(config)
                sr = search_client.search_resources(
                    search_details=oci.resource_search.models.FreeTextSearchDetails(text=resource_id)
                )
                if sr.data.items:
                    comp_id = sr.data.items[0].compartment_id
                    bs_client = oci.core.BlockstorageClient(config)
                    resp = bs_client.delete_volume(resource_id)
                    return True, f"Volume delete initiated..."
                return False, "Could not find compartment"
            else:
                return False, "Block Storage does not support start/stop"

        elif rtype == "network":
            if action == "delete":
                search_client = oci.resource_search.ResourceSearchClient(config)
                sr = search_client.search_resources(
                    search_details=oci.resource_search.models.FreeTextSearchDetails(text=resource_id)
                )
                if sr.data.items:
                    comp_id = sr.data.items[0].compartment_id
                    # Could be VCN or subnet
                    if "vcn" in resource_id.lower():
                        net_client = oci.core.VirtualNetworkClient(config)
                        resp = net_client.delete_vcn(resource_id)
                        return True, f"VCN delete initiated..."
                    elif "subnetwork" in resource_id.lower():
                        net_client = oci.core.VirtualNetworkClient(config)
                        resp = net_client.delete_subnet(resource_id)
                        return True, f"Subnet delete initiated..."
                return False, "Could not determine network resource type"
            else:
                return False, "Network resources do not support start/stop"

        elif rtype == "oke":
            if action == "delete":
                search_client = oci.resource_search.ResourceSearchClient(config)
                sr = search_client.search_resources(
                    search_details=oci.resource_search.models.FreeTextSearchDetails(text=resource_id)
                )
                if sr.data.items:
                    comp_id = sr.data.items[0].compartment_id
                    oke_client = oci.container_engine.ContainerEngineClient(config)
                    resp = oke_client.delete_cluster(resource_id)
                    return True, f"OKE Cluster delete initiated..."
                return False, "Could not find compartment"
            else:
                return False, "OKE does not support start/stop via this tool"

        elif rtype == "file_storage":
            if action == "delete":
                return False, "File Storage deletion not supported via this tool (use OCI Console)"
            else:
                return False, "File Storage does not support start/stop"

        else:
            return False, f"Resource type '{rtype}' actions not supported yet"

    except oci.exceptions.ServiceError as e:
        return False, f"OCI Error: {e.message}"
    except Exception as e:
        return False, f"Error: {str(e)}"

    return False, "Unsupported action"


@app.route("/api/resource/action", methods=["POST"])
@admin_required
def api_resource_action():
    data = request.get_json()
    resource_id = data.get("resource_id", "")
    action = data.get("action", "")

    if not resource_id or action not in ("start", "stop", "delete"):
        return jsonify({"success": False, "message": "Invalid parameters"})

    success, message = _resource_action(resource_id, action)
    return jsonify({"success": success, "message": message})


if __name__ == "__main__":
    print("=" * 60)
    print("OCI Cost Dashboard")
    print("Open http://localhost:5000 in your browser")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
