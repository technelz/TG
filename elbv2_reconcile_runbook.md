# ELBv2 DR Routing Reconciliation Runbook

## Purpose

This runbook supports `elbv2_reconcile_engine.py`.

The script compares and optionally reconciles the ELBv2 routing chain between a source/Prod AWS account and a target/DR AWS account.

It validates more than Target Group parity. It checks:

- Load Balancers
- Load Balancer attributes
- Listeners
- Listener default actions
- Listener rules
- Target Groups
- Target Group settings
- Target Group attributes
- Tags
- ACM certificate domain matching
- Target Group association through listener actions

It intentionally does not register EC2 instances, ECS tasks, Lambda targets, or IP targets because DR compute may not be restored yet.

---

## Important Concept

A Target Group being in sync does not mean DR routing is ready.

DR routing parity means:

```text
Load Balancer exists
  -> Listener exists
    -> Listener rule exists
      -> Rule/default action forwards to correct DR Target Group
```

Registered targets are skipped when DR instances are not restored.

---

## Required Info File

Use `info.json`:

```json
{
  "source": {
    "profile": "prod",
    "region": "us-east-1",
    "vpc_id": "vpc-prod"
  },
  "target": {
    "profile": "dr",
    "region": "us-east-2",
    "vpc_id": "vpc-dr"
  },
  "policy": {
    "register_targets": false,
    "create_missing_load_balancers": false,
    "create_missing_target_groups": true,
    "sync_load_balancer_attributes": true,
    "sync_target_group_attributes": true,
    "sync_target_group_settings": true,
    "sync_listener_rules": true,
    "sync_listeners": true,
    "sync_tags": true,
    "allow_extra_target_rules": true,
    "skip_target_registration": true,
    "allow_certificate_domain_match": true
  }
}
```

Do not list every Target Group or certificate manually. The script discovers them dynamically.

---

## Recommended Execution Flow

### 1. Confirm AWS SSO/login

```bash
aws sso login --profile prod
aws sso login --profile dr
```

Or confirm existing credentials:

```bash
aws sts get-caller-identity --profile prod
aws sts get-caller-identity --profile dr
```

---

### 2. First dry run without legacy matching

```bash
python elbv2_reconcile_engine.py \
  --info-file info.json \
  --dry-run
```

This checks only exact resource-name matches.

---

### 3. Dry run with normalized fallback matching

```bash
python elbv2_reconcile_engine.py \
  --info-file info.json \
  --allow-legacy \
  --dry-run
```

Use this when Prod and DR names differ because of:

- prod/dr prefixes
- CloudFormation suffixes
- random hashes
- environment labels

The script reports ambiguous matches instead of silently applying them.

---

### 4. Review the report

Reports are written to:

```text
elbv2_reports/
```

Check:

```text
summary
matches
ambiguous_matches
unmatched
target_group_results
load_balancer_results
listener_results
listener_rule_results
execution
post_apply_validation
```

---

### 5. Apply only after review

```bash
python elbv2_reconcile_engine.py \
  --info-file info.json \
  --allow-legacy \
  --yes
```

---

## What the Script Can Apply

The script can safely apply:

- Missing Target Groups
- Target Group health-check settings
- Target Group supported attributes
- Source-required tags
- Load Balancer supported mutable attributes
- Listener updates
- Listener default action updates
- Listener rule creates/updates
- Forward action remapping from source TG ARN to DR TG ARN
- ACM certificate remapping by matching domain/SAN

---

## What the Script Does Not Do Automatically

The script does not automatically:

- Delete Target Groups
- Delete Load Balancers
- Register EC2 instances
- Register ECS tasks
- Register Lambda targets
- Register IP targets
- Recreate immutable Target Groups
- Recreate immutable Load Balancers
- Blindly copy certificate ARNs across accounts/regions
- Guess subnet mappings
- Guess security group mappings

---

## Expected Report Interpretation

### Good DR routing state

```text
target_groups_in_sync = total target groups
load_balancers_in_sync = total load balancers
listeners_in_sync = total listeners
listener_rules_in_sync = total listener rules
create_total = 0
update_total = 0
manual_review_total = 0
ambiguous_matches = 0
```

### Acceptable warning while DR instances are not restored

```text
Target registration: SKIPPED_DR_INSTANCES_NOT_RESTORED
```

This is expected.

### Real issue

```text
Load Balancer: MANUAL_REVIEW
Listener: CREATE
Listener rule: CREATE
No target TG mapping
No DR ACM certificate match
```

These mean the routing chain is incomplete.

---

## Console Validation After Apply

In the DR AWS Console, verify:

1. EC2 -> Load Balancers
2. Select the DR ALB/NLB
3. Check Listeners
4. Check each listener default action
5. Check listener rules
6. Confirm rules forward to DR Target Groups
7. Go to Target Groups
8. Confirm each TG shows Load Balancer association
9. Ignore missing targets if DR instances are not restored

---

## Best Practice

Run in this order:

```bash
python elbv2_reconcile_engine.py --info-file info.json --dry-run

python elbv2_reconcile_engine.py --info-file info.json --allow-legacy --dry-run

python elbv2_reconcile_engine.py --info-file info.json --allow-legacy --yes
```

Do not run `--yes` if the dry-run report shows ambiguous matches or unresolved certificate/TG mappings.
