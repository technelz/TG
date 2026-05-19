#!/usr/bin/env python3
from __future__ import annotations

"""
tg_reconcile_engine.py

Purpose
-------
Reconcile AWS Target Groups from a source environment into a target environment
using a lightweight info file that ONLY contains environment/account metadata.

Example tg_config.json
----------------------

{
  "source": {
    "profile": "prod",
    "region": "us-east-1",
    "vpc_id": "vpc-111"
  },

  "target": {
    "profile": "stage",
    "region": "us-east-2",
    "vpc_id": "vpc-222"
  }
}

Usage
-----

Dry run:

python tg_reconcile_engine.py \
  --info-file tg_config.json \
  --dry-run

Apply:

python tg_reconcile_engine.py \
  --info-file tg_config.json

Audit only:

python tg_reconcile_engine.py \
  --info-file tg_config.json \
  --report-only
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError


# =============================================================================
# constants
# =============================================================================

SUPPORTED_ATTR_KEYS = [
    "deregistration_delay.timeout_seconds",
    "stickiness.enabled",
    "stickiness.type",
    "stickiness.lb_cookie.duration_seconds",
    "slow_start.duration_seconds",
    "load_balancing.algorithm.type",
]

IMMUTABLE_FIELDS = [
    "Protocol",
    "Port",
    "TargetType",
    "IpAddressType",
    "ProtocolVersion",
]

MUTABLE_FIELDS = [
    "HealthCheckProtocol",
    "HealthCheckPort",
    "HealthCheckEnabled",
    "HealthCheckPath",
    "HealthCheckIntervalSeconds",
    "HealthCheckTimeoutSeconds",
    "HealthyThresholdCount",
    "UnhealthyThresholdCount",
    "Matcher",
]


# =============================================================================
# logging
# =============================================================================

def log(msg: str):
    print(msg, flush=True)


def eprint(msg: str):
    print(msg, file=sys.stderr, flush=True)


# =============================================================================
# models
# =============================================================================

@dataclass
class Environment:
    profile: str
    region: str
    vpc_id: str


@dataclass
class Config:
    source: Environment
    target: Environment


@dataclass
class AuditResult:
    name: str

    exists: bool = False

    immutable_match: bool = True
    mutable_match: bool = True
    attribute_match: bool = True

    immutable_drift: List[str] = field(default_factory=list)
    mutable_drift: List[str] = field(default_factory=list)
    attribute_drift: List[str] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)

    @property
    def in_sync(self):
        return (
            self.exists
            and self.immutable_match
            and self.mutable_match
            and self.attribute_match
        )


# =============================================================================
# config
# =============================================================================

def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return Config(
        source=Environment(**data["source"]),
        target=Environment(**data["target"]),
    )


# =============================================================================
# aws
# =============================================================================

def get_elbv2(profile: str, region: str):
    session = boto3.Session(
        profile_name=profile,
        region_name=region,
    )

    return session.client("elbv2")


def describe_target_groups_by_vpc(elbv2, vpc_id: str):
    paginator = elbv2.get_paginator(
        "describe_target_groups"
    )

    out = []

    for page in paginator.paginate(PageSize=400):
        for tg in page["TargetGroups"]:
            if tg["VpcId"] == vpc_id:
                out.append(tg)

    return out


def describe_target_group_attributes(
    elbv2,
    tg_arn: str,
):
    resp = elbv2.describe_target_group_attributes(
        TargetGroupArn=tg_arn
    )

    return {
        x["Key"]: x.get("Value", "")
        for x in resp["Attributes"]
        if x["Key"] in SUPPORTED_ATTR_KEYS
    }


# =============================================================================
# normalization
# =============================================================================

def normalize_matcher(matcher):
    if not matcher:
        return {}

    out = {}

    if matcher.get("HttpCode") is not None:
        out["HttpCode"] = matcher["HttpCode"]

    if matcher.get("GrpcCode") is not None:
        out["GrpcCode"] = matcher["GrpcCode"]

    return out


def normalize_target_group(
    tg: Dict[str, Any],
    attrs: Dict[str, str],
):
    return {
        "TargetGroupArn": tg.get("TargetGroupArn"),

        "TargetGroupName": tg["TargetGroupName"],

        "Protocol": tg.get("Protocol"),
        "Port": tg.get("Port"),
        "TargetType": tg.get("TargetType"),

        "IpAddressType": tg.get("IpAddressType"),
        "ProtocolVersion": tg.get("ProtocolVersion"),

        "HealthCheckProtocol": tg.get("HealthCheckProtocol"),
        "HealthCheckPort": tg.get("HealthCheckPort"),
        "HealthCheckEnabled": tg.get("HealthCheckEnabled"),
        "HealthCheckPath": tg.get("HealthCheckPath"),

        "HealthCheckIntervalSeconds": tg.get("HealthCheckIntervalSeconds"),
        "HealthCheckTimeoutSeconds": tg.get("HealthCheckTimeoutSeconds"),

        "HealthyThresholdCount": tg.get("HealthyThresholdCount"),
        "UnhealthyThresholdCount": tg.get("UnhealthyThresholdCount"),

        "Matcher": normalize_matcher(
            tg.get("Matcher")
        ),

        "Attributes": attrs,
    }


# =============================================================================
# discovery
# =============================================================================

def discover_normalized_target_groups(
    profile: str,
    region: str,
    vpc_id: str,
):
    elbv2 = get_elbv2(profile, region)

    raw_tgs = describe_target_groups_by_vpc(
        elbv2,
        vpc_id,
    )

    normalized = {}

    for tg in raw_tgs:
        attrs = describe_target_group_attributes(
            elbv2,
            tg["TargetGroupArn"],
        )

        normalized[
            tg["TargetGroupName"]
        ] = normalize_target_group(
            tg,
            attrs,
        )

    return normalized


# =============================================================================
# diff
# =============================================================================

def diff_fields(
    source: Dict[str, Any],
    target: Dict[str, Any],
    fields: List[str],
):
    return [
        field
        for field in fields
        if source.get(field) != target.get(field)
    ]


# =============================================================================
# sync
# =============================================================================

def create_target_group(
    elbv2,
    desired: Dict[str, Any],
    target_vpc_id: str,
    dry_run: bool,
):
    name = desired["TargetGroupName"]

    if dry_run:
        log(f"[DRY-RUN] Would create target group: {name}")
        return

    payload = {
        "Name": name,
        "Protocol": desired["Protocol"],
        "Port": desired["Port"],
        "VpcId": target_vpc_id,
        "TargetType": desired.get("TargetType", "instance"),
    }

    if desired.get("ProtocolVersion"):
        payload["ProtocolVersion"] = desired["ProtocolVersion"]

    if desired.get("IpAddressType"):
        payload["IpAddressType"] = desired["IpAddressType"]

    for key in MUTABLE_FIELDS:
        value = desired.get(key)

        if value is None or value == {}:
            continue

        payload[key] = value

    resp = elbv2.create_target_group(**payload)

    tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]

    log(f"[CREATE] {name} -> {tg_arn}")

    attrs = desired.get("Attributes") or {}

    if attrs:
        modify_target_group_attributes(
            elbv2,
            tg_arn,
            attrs,
            dry_run=False,
        )


def modify_target_group(
    elbv2,
    desired: Dict[str, Any],
    target_arn: str,
    dry_run: bool,
):
    payload = {
        "TargetGroupArn": target_arn,
    }

    for key in MUTABLE_FIELDS:
        value = desired.get(key)

        if value is None or value == {}:
            continue

        payload[key] = value

    if len(payload) == 1:
        return

    if dry_run:
        log(f"[DRY-RUN] Would modify TG settings: {target_arn}")
        return

    elbv2.modify_target_group(**payload)


def modify_target_group_attributes(
    elbv2,
    target_arn: str,
    desired_attrs: Dict[str, str],
    dry_run: bool,
):
    if not desired_attrs:
        return

    payload = [
        {
            "Key": k,
            "Value": v,
        }
        for k, v in sorted(desired_attrs.items())
    ]

    if dry_run:
        log(f"[DRY-RUN] Would modify TG attributes: {target_arn}")
        return

    elbv2.modify_target_group_attributes(
        TargetGroupArn=target_arn,
        Attributes=payload,
    )


# =============================================================================
# audit
# =============================================================================

def audit_target_group(
    desired: Dict[str, Any],
    live: Optional[Dict[str, Any]],
):
    result = AuditResult(
        name=desired["TargetGroupName"]
    )

    if not live:
        result.exists = False
        result.notes.append(
            "Missing in target environment."
        )
        return result

    result.exists = True

    immutable_drift = diff_fields(
        desired,
        live,
        IMMUTABLE_FIELDS,
    )

    if immutable_drift:
        result.immutable_match = False
        result.immutable_drift.extend(
            immutable_drift
        )

    mutable_drift = diff_fields(
        desired,
        live,
        MUTABLE_FIELDS,
    )

    if mutable_drift:
        result.mutable_match = False
        result.mutable_drift.extend(
            mutable_drift
        )

    desired_attrs = desired.get("Attributes", {})
    live_attrs = live.get("Attributes", {})

    attr_drift = sorted(
        key
        for key in (
            set(desired_attrs.keys())
            | set(live_attrs.keys())
        )
        if desired_attrs.get(key)
        != live_attrs.get(key)
    )

    if attr_drift:
        result.attribute_match = False
        result.attribute_drift.extend(
            attr_drift
        )

    return result


def print_report(results: List[AuditResult]):
    print("\n" + "=" * 80)
    print("TARGET GROUP RECONCILIATION REPORT")
    print("=" * 80)

    in_sync = [x for x in results if x.in_sync]
    drifted = [x for x in results if not x.in_sync]

    print(f"Total TGs       : {len(results)}")
    print(f"In sync         : {len(in_sync)}")
    print(f"Needs attention : {len(drifted)}")

    if not drifted:
        print("\nAll target groups are in sync.")
        print("=" * 80)
        return

    for r in drifted:
        print("\n" + "-" * 80)
        print(f"TG: {r.name}")

        print(f"Exists        : {r.exists}")
        print(f"Immutable     : {'OK' if r.immutable_match else 'DRIFT'}")
        print(f"Mutable       : {'OK' if r.mutable_match else 'DRIFT'}")
        print(f"Attributes    : {'OK' if r.attribute_match else 'DRIFT'}")

        if r.immutable_drift:
            print("Immutable drift:")
            for x in r.immutable_drift:
                print(f"  - {x}")

        if r.mutable_drift:
            print("Mutable drift:")
            for x in r.mutable_drift:
                print(f"  - {x}")

        if r.attribute_drift:
            print("Attribute drift:")
            for x in r.attribute_drift:
                print(f"  - {x}")

        if r.notes:
            print("Notes:")
            for x in r.notes:
                print(f"  - {x}")

    print("=" * 80)


# =============================================================================
# args
# =============================================================================

def parse_args():
    p = argparse.ArgumentParser()

    p.add_argument(
        "--info-file",
        required=True,
    )

    p.add_argument(
        "--dry-run",
        action="store_true",
    )

    p.add_argument(
        "--report-only",
        action="store_true",
    )

    return p.parse_args()


# =============================================================================
# main
# =============================================================================

def main():
    args = parse_args()

    config = load_config(
        args.info_file
    )

    log(
        f"[INFO] Discovering source target groups "
        f"({config.source.profile}/{config.source.region})"
    )

    source_tgs = discover_normalized_target_groups(
        config.source.profile,
        config.source.region,
        config.source.vpc_id,
    )

    log(
        f"[INFO] Discovering target target groups "
        f"({config.target.profile}/{config.target.region})"
    )

    target_tgs = discover_normalized_target_groups(
        config.target.profile,
        config.target.region,
        config.target.vpc_id,
    )

    target_elbv2 = get_elbv2(
        config.target.profile,
        config.target.region,
    )

    audit_results = []

    for tg_name, desired in sorted(source_tgs.items()):
        live = target_tgs.get(tg_name)

        audit = audit_target_group(
            desired,
            live,
        )

        audit_results.append(audit)

        if args.report_only:
            continue

        if not live:
            try:
                create_target_group(
                    target_elbv2,
                    desired,
                    config.target.vpc_id,
                    args.dry_run,
                )
            except ClientError as e:
                eprint(
                    f"[ERROR] Failed creating "
                    f"{tg_name}: {e}"
                )

            continue

        if audit.mutable_drift:
            try:
                log(
                    f"[SYNC] Updating mutable settings "
                    f"for {tg_name}"
                )

                target_arn = live.get("TargetGroupArn")

                if not target_arn:
                    eprint(
                        f"[ERROR] Missing TargetGroupArn "
                        f"for live target group {tg_name}"
                    )
                    continue

                target_arn = live.get("TargetGroupArn")

                if not target_arn:
                    eprint(
                        f"[ERROR] Missing TargetGroupArn "
                        f"for {tg_name}"
                    )
                    continue

                target_arn = live.get("TargetGroupArn")

                if not target_arn:
                    eprint(
                        f"[ERROR] Missing TargetGroupArn "
                        f"for {tg_name}"
                    )
                    continue

                modify_target_group_attributes(
                    target_elbv2,
                    target_arn,
                    desired["Attributes"],
                    args.dry_run,
                )

            except ClientError as e:
                eprint(
                    f"[ERROR] Failed updating "
                    f"{tg_name}: {e}"
                )

        if audit.attribute_drift:
            try:
                log(
                    f"[SYNC] Updating attributes "
                    f"for {tg_name}"
                )

                target_arn = live.get("TargetGroupArn")

                if not target_arn:
                    eprint(
                        f"[ERROR] Missing TargetGroupArn "
                        f"for {tg_name}"
                    )
                    continue

                modify_target_group(
                    target_elbv2,
                    desired,
                    target_arn,
                    args.dry_run,
                )

            except ClientError as e:
                eprint(
                    f"[ERROR] Failed updating attrs "
                    f"for {tg_name}: {e}"
                )

        if audit.immutable_drift:
            log(
                f"[WARN] Immutable drift detected "
                f"for {tg_name}: "
                f"{', '.join(audit.immutable_drift)}"
            )

    print_report(audit_results)

    log("[DONE] Target group reconciliation complete.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
