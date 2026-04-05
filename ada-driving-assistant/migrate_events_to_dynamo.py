#!/usr/bin/env python3
"""
migrate_events_to_dynamo.py — One-time migration of city_objects.json files
from S3 into the ada-events DynamoDB table.

Run after deploying the updated CloudFormation stack (which creates the table):
    python migrate_events_to_dynamo.py

Optional flags:
    --bucket    S3 bucket name (default: ada-driving-assistant)
    --region    AWS region    (default: us-west-2)
    --dry-run   Print events without writing to DynamoDB
"""

import argparse
import json
import os

import boto3

from events import put_event

CITIES = [
    ("Berkeley",   "CA/Berkeley"),
    ("Albany",     "CA/Albany"),
    ("ElCerrito",  "CA/ElCerrito"),
    ("Richmond",   "CA/Richmond"),
    ("Emeryville", "CA/Emeryville"),
    ("Oakland",    "CA/Oakland"),
]


def migrate(bucket: str, region: str, dry_run: bool):
    s3 = boto3.client("s3", region_name=region)
    total_written = 0

    for city, prefix in CITIES:
        key = f"{prefix}/city_objects.json"
        print(f"\n==> {city} (s3://{bucket}/{key})")
        try:
            resp   = s3.get_object(Bucket=bucket, Key=key)
            events = json.loads(resp["Body"].read().decode("utf-8"))
        except s3.exceptions.NoSuchKey:
            print(f"    [skip] key not found")
            continue
        except Exception as exc:
            print(f"    [error] {exc}")
            continue

        print(f"    {len(events)} events found")
        written = 0
        skipped = 0

        for ev in events:
            # Ensure city field is present for the city-index GSI
            if "city" not in ev:
                ev["city"] = city

            if dry_run:
                written += 1
                continue

            try:
                put_event(ev)
                written += 1
            except Exception as exc:
                print(f"    [warn] could not write event {ev.get('event_id','?')}: {exc}")
                skipped += 1

        total_written += written
        action = "would write" if dry_run else "wrote"
        print(f"    {action} {written} events, skipped {skipped}")

    print(f"\nDone. Total events {'would be written' if dry_run else 'written'}: {total_written}")


def main():
    parser = argparse.ArgumentParser(description="Migrate S3 city_objects.json to DynamoDB")
    parser.add_argument("--bucket",  default=os.environ.get("S3_BUCKET", "ada-driving-assistant"))
    parser.add_argument("--region",  default=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args()

    print(f"Migrating from s3://{args.bucket} to DynamoDB table 'ada-events' ({args.region})")
    if args.dry_run:
        print("[DRY RUN — no writes will occur]")

    migrate(args.bucket, args.region, args.dry_run)


if __name__ == "__main__":
    main()
