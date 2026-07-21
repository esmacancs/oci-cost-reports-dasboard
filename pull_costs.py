#!/usr/bin/env python3
"""
OCI Cost Reports Extractor
Uses the Usage API to pull cost data and exports to Excel.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import oci
import pandas as pd


def get_config():
    config = oci.config.from_file(
        file_location="E:\\oci-cost-reports\\oci_config",
        profile_name="DEFAULT"
    )
    oci.config.validate_config(config)
    return config


def pull_cost_data(config):
    """Pull cost data using the Usage API."""
    usage_client = oci.usage_api.UsageapiClient(config)
    
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)
    
    print(f"\n  Requesting summarized usage from {start_date.date()} to {end_date.date()}...")
    
    request = oci.usage_api.models.RequestSummarizedUsagesDetails(
        tenant_id=config['tenancy'],
        time_usage_started=start_date.strftime('%Y-%m-%dT00:00:00.000Z'),
        time_usage_ended=end_date.strftime('%Y-%m-%dT00:00:00.000Z'),
        granularity="DAILY",
        group_by=["service", "compartmentName"],
        compartment_depth=1
    )
    
    try:
        response = usage_client.request_summarized_usages(request)
        return response.data
    except oci.exceptions.ServiceError as e:
        print(f"  Service Error: {e.message}")
        print(f"  Status: {e.status}")
        print(f"  Code: {e.code}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None


def process_usage_data(raw_data):
    """Parse the Usage API response into a clean DataFrame."""
    rows = []
    for item in raw_data.items:
        row = {
            'service': item.service,
            'compartment_name': item.compartment_name,
            'time_usage_started': item.time_usage_started,
            'time_usage_ended': item.time_usage_ended,
            'computed_amount': item.computed_amount,
            'computed_quantity': item.computed_quantity,
            'currency': item.currency,
            'attributed_cost': item.attributed_cost,
            'attributed_usage': item.attributed_usage,
            'is_forecast': item.is_forecast,
            'resource_id': item.resource_id,
            'resource_name': item.resource_name,
            'unit': item.unit,
        }
        rows.append(row)
    
    df = pd.DataFrame(rows)
    
    # Convert numeric columns
    for col in ['computed_amount', 'attributed_cost', 'computed_quantity', 'attributed_usage']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    
    # Parse dates as strings to avoid tz issues in Excel
    for col in ['time_usage_started', 'time_usage_ended']:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace(' ', 'T').str.split('+').str[0]
    
    return df


def export_to_excel(df, output_path):
    """Export data to Excel with summary sheets."""
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        # Main data
        df.to_excel(writer, sheet_name='Usage_Data', index=False)
        
        # Summary by service
        if 'service' in df.columns and 'computed_amount' in df.columns:
            svc = df.groupby('service').agg(
                total_cost=('computed_amount', 'sum'),
                total_usage=('computed_quantity', 'sum'),
                days=('time_usage_started', 'count')
            ).sort_values('total_cost', ascending=False)
            svc.to_excel(writer, sheet_name='By_Service')
        
        # Summary by compartment
        if 'compartment_name' in df.columns and 'computed_amount' in df.columns:
            comp = df.groupby('compartment_name').agg(
                total_cost=('computed_amount', 'sum'),
                total_usage=('computed_quantity', 'sum'),
                days=('time_usage_started', 'count')
            ).sort_values('total_cost', ascending=False)
            comp.to_excel(writer, sheet_name='By_Compartment')
        
        # Daily cost
        if 'time_usage_started' in df.columns and 'computed_amount' in df.columns:
            df['date'] = df['time_usage_started'].astype(str).str[:10]
            daily = df.groupby('date').agg(
                total_cost=('computed_amount', 'sum')
            ).reset_index()
            daily.to_excel(writer, sheet_name='Daily_Cost', index=False)
    
    print(f"\n  Excel saved to: {output_path}")


def main():
    print("=" * 60)
    print("OCI Cost Reports Extractor")
    print("=" * 60)
    
    print("\n[1/2] Loading OCI configuration...")
    config = get_config()
    print(f"  Tenancy: {config['tenancy']}")
    print(f"  Region: {config['region']}")
    
    print("\n[2/2] Pulling cost data from Usage API...")
    raw_data = pull_cost_data(config)
    
    if not raw_data or not raw_data.items:
        print("\n  No cost data found.")
        return
    
    print(f"  Got {len(raw_data.items)} records. Processing...")
    
    df = process_usage_data(raw_data)
    
    print(f"\n  Total records: {len(df)}")
    print(f"  Services: {df['service'].nunique()}")
    print(f"  Compartments: {df['compartment_name'].nunique()}")
    
    total_cost = df['computed_amount'].sum()
    print(f"\n  TOTAL COST (last 30 days): ${total_cost:,.2f} USD")
    
    print(f"\n  Cost by service:")
    for svc, cost in df.groupby('service')['computed_amount'].sum().sort_values(ascending=False).items():
        print(f"    {svc}: ${cost:,.2f}")
    
    print(f"\n  Cost by compartment:")
    for comp, cost in df.groupby('compartment_name')['computed_amount'].sum().sort_values(ascending=False).items():
        print(f"    {comp}: ${cost:,.2f}")
    
    output_dir = "E:\\oci-cost-reports\\output"
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "oci_cost_report.xlsx")
    export_to_excel(df, output_path)
    
    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)


if __name__ == "__main__":
    main()
