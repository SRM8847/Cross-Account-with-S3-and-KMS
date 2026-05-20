# Cross-Account S3 + KMS — Complete Guide
**Account A:** (fully upgraded) | **Account B:** (free tier)
**Region:** us-east-1 | **Goal:** Account A assumes a role in Account B to access an encrypted S3 bucket

---

## Charge Summary

| Account | Resource | Cost |
|---------|----------|------|
| Account A | STS AssumeRole, IAM | **$0 — always free** |
| Account B | S3 bucket | Free tier (5GB, 20K GETs, 2K PUTs) |
| Account B | KMS CMK | ~$0.03 if deleted same day |
| Account B | KMS API calls | ~$0.00 (< 50 calls) |

**Account A pays nothing. Account B pays essentially $0.**

---

## Architecture

```
Account A                    Account B
──────────────────                    ──────────────────────────────────────
IAM User: account-a-user                  IAM Role: CrossAccountS3Role
  └─ Policy: sts:AssumeRole ───────►    └─ Trust policy: allows Account A
                                         └─ Permissions: S3 + KMS

                                      S3 Bucket: account-b-crossacct-demo-<random>
                                        └─ Default encryption: SSE-KMS
                                        └─ Versioning: Enabled
                                        └─ Bucket policy: DenyNonSSL + DenyUnencrypted
                                        └─ Access logging → account-b-crossacct-logs-<random>

                                      KMS CMK: alias/s3-crossaccount-key
                                        └─ Key policy: allows root + CrossAccountS3Role
```

**Flow:** `Account A CLI → STS AssumeRole (with ExternalId) → temp creds → Account B role → S3 + KMS`

---

## Build Order

```
Phase 1 → S3 Buckets (Account B)
Phase 2 → IAM Role (Account B)       ← must exist before KMS key policy references it
Phase 3 → KMS Key (Account B)        ← now key policy can safely name the role
Phase 4 → S3 Encryption + Bucket Policy (Account B)
Phase 5 → Update Role Policy with real KMS ARN (Account B)
Phase 6 → AssumeRole Permission (Account A)
Phase 7 → CLI Profile Setup
Phase 8 → Testing
Phase 9 → Python boto3 Script
```

---

## Pre-requisites

```bash
# Confirm both profiles are working
aws sts get-caller-identity --profile account-a
aws sts get-caller-identity --profile account-b

# Note both Account IDs — you will substitute them throughout
aws sts get-caller-identity --query Account --output text --profile account-a   # ACCOUNT_A_ID
aws sts get-caller-identity --query Account --output text --profile account-b   # ACCOUNT_B_ID
```

---

## Phase 1 — Account B: Create S3 Buckets

> **Critical note for us-east-1:** Do NOT use `--create-bucket-configuration LocationConstraint`.
> us-east-1 is the default region — AWS throws `InvalidLocationConstraint` if you pass it.
> All other regions require that flag. us-east-1 does not.

### 1.1 Create the main bucket

Pick a globally unique name. Add your name + a random number as suffix.

```bash
aws s3api create-bucket \
  --bucket account-b-crossacct-demo-<random> \
  --region us-east-1 \
  --profile account-b
```

### 1.2 Create the logging bucket

```bash
aws s3api create-bucket \
  --bucket account-b-crossacct-logs-<random> \
  --region us-east-1 \
  --profile account-b
```

### 1.3 Grant S3 log delivery permission on the logging bucket

Without this, access logs silently fail to deliver. Create `logging-bucket-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3LogDeliveryWrite",
      "Effect": "Allow",
      "Principal": {
        "Service": "logging.s3.amazonaws.com"
      },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::account-b-crossacct-logs-<random>/access-logs/*",
      "Condition": {
        "ArnLike": {
          "aws:SourceArn": "arn:aws:s3:::account-b-crossacct-demo-<random>"
        }
      }
    }
  ]
}
```

```bash
aws s3api put-bucket-policy \
  --bucket account-b-crossacct-logs-<random> \
  --policy file://logging-bucket-policy.json \
  --profile account-b
```

### 1.4 Block public access on both buckets

```bash
aws s3api put-public-access-block \
  --bucket account-b-crossacct-demo-<random> \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
  --profile account-b

aws s3api put-public-access-block \
  --bucket account-b-crossacct-logs-<random> \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
  --profile account-b
```

### 1.5 Enable versioning on main bucket

```bash
aws s3api put-bucket-versioning \
  --bucket account-b-crossacct-demo-<random> \
  --versioning-configuration Status=Enabled \
  --profile account-b
```

### 1.6 Enable access logging on main bucket

```bash
aws s3api put-bucket-logging \
  --bucket account-b-crossacct-demo-<random> \
  --bucket-logging-status '{
    "LoggingEnabled": {
      "TargetBucket": "account-b-crossacct-logs-<random>",
      "TargetPrefix": "access-logs/"
    }
  }' \
  --profile account-b
```

---

## Phase 2 — Account B: Create IAM Role

### 2.1 Create trust policy

`trust-policy.json` — replace `<ACCOUNT_A_ID>` with Account A's actual ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<ACCOUNT_A_ID>:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "CrossAcctS3Demo"
        }
      }
    }
  ]
}
```

The `ExternalId` condition protects against confused deputy attacks — a real-world best practice
worth knowing for interviews.

```bash
aws iam create-role \
  --role-name CrossAccountS3Role \
  --assume-role-policy-document file://trust-policy.json \
  --description "Allows Account A to access Account B S3 bucket and KMS key" \
  --profile account-b
```

**Save the `RoleArn` from the output.**

### 2.2 Create permission policy (placeholder KMS ARN for now)

`role-permissions.json` — fill in the bucket name, leave KMS ARN as placeholder for now:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3BucketAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration"
      ],
      "Resource": [
        "arn:aws:s3:::account-b-crossacct-demo-<random>",
        "arn:aws:s3:::account-b-crossacct-demo-<random>/*"
      ]
    },
    {
      "Sid": "KMSKeyAccess",
      "Effect": "Allow",
      "Action": [
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "kms:DescribeKey"
      ],
      "Resource": "PLACEHOLDER_UPDATE_AFTER_PHASE_3"
    }
  ]
}
```

```bash
aws iam put-role-policy \
  --role-name CrossAccountS3Role \
  --policy-name S3KMSAccessPolicy \
  --policy-document file://role-permissions.json \
  --profile account-b
```

---

## Phase 3 — Account B: Create KMS Key

Now that the role exists, the key policy can safely reference it.

### 3.1 Create the key

```bash
aws kms create-key \
  --description "Cross-account S3 encryption key" \
  --key-usage ENCRYPT_DECRYPT \
  --region us-east-1 \
  --profile account-b
```

**Copy the `KeyId` and full `KeyArn` from the output. You will use both.**

### 3.2 Create alias

```bash
aws kms create-alias \
  --alias-name alias/s3-crossaccount-key \
  --target-key-id <KeyId> \
  --region us-east-1 \
  --profile account-b
```

### 3.3 Apply full key policy

`kms-key-policy.json` — replace both Account B IDs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EnableAccountBRootAccess",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<ACCOUNT_B_ID>:root"
      },
      "Action": "kms:*",
      "Resource": "*"
    },
    {
      "Sid": "AllowCrossAccountRoleToUseKey",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<ACCOUNT_B_ID>:role/CrossAccountS3Role"
      },
      "Action": [
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "kms:DescribeKey"
      ],
      "Resource": "*"
    }
  ]
}
```

```bash
aws kms put-key-policy \
  --key-id <KeyId> \
  --policy-name default \
  --policy file://kms-key-policy.json \
  --region us-east-1 \
  --profile account-b
```

---

## Phase 4 — Account B: Enable Bucket Encryption and Apply Bucket Policy

### 4.1 Enable SSE-KMS default encryption on main bucket

`BucketKeyEnabled: true` reduces KMS API calls per object — always set this.

```bash
aws s3api put-bucket-encryption \
  --bucket account-b-crossacct-demo-<random> \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": "<KMS_KEY_ARN>"
      },
      "BucketKeyEnabled": true
    }]
  }' \
  --profile account-b
```

### 4.2 Apply bucket policy

`bucket-policy.json` — two statements: deny HTTP, deny unencrypted uploads:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyNonSSLRequests",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::account-b-crossacct-demo-<random>",
        "arn:aws:s3:::account-b-crossacct-demo-<random>/*"
      ],
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    },
    {
      "Sid": "DenyUnencryptedObjectUploads",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::account-b-crossacct-demo-<random>/*",
      "Condition": {
        "StringNotEquals": {
          "s3:x-amz-server-side-encryption": "aws:kms"
        }
      }
    }
  ]
}
```

```bash
aws s3api put-bucket-policy \
  --bucket account-b-crossacct-demo-<random> \
  --policy file://bucket-policy.json \
  --profile account-b
```

---

## Phase 5 — Account B: Update Role Policy with Real KMS ARN

Now replace the placeholder in `role-permissions.json` with the actual `<KMS_KEY_ARN>`, then re-apply:

```bash
aws iam put-role-policy \
  --role-name CrossAccountS3Role \
  --policy-name S3KMSAccessPolicy \
  --policy-document file://role-permissions.json \
  --profile account-b
```

Verify it looks correct:

```bash
aws iam get-role-policy \
  --role-name CrossAccountS3Role \
  --policy-name S3KMSAccessPolicy \
  --profile account-b
```

---

## Phase 6 — Account A: Grant AssumeRole Permission

This is the only thing done in Account A.

`assume-role-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::<ACCOUNT_B_ID>:role/CrossAccountS3Role"
    }
  ]
}
```

```bash
aws iam put-user-policy \
  --user-name <your-iam-username-in-account-a> \
  --policy-name AssumeAccountBRole \
  --policy-document file://assume-role-policy.json \
  --profile account-a
```

---

## Phase 7 — CLI Profile Setup

### 7.1 Verify both base profiles

```bash
aws sts get-caller-identity --profile account-a
aws sts get-caller-identity --profile account-b
```

### 7.2 Add chained profile to `~/.aws/config`

```ini
[profile crossacct-s3]
role_arn = arn:aws:iam::<ACCOUNT_B_ID>:role/CrossAccountS3Role
source_profile = account-a
external_id = CrossAcctS3Demo
region = us-east-1
```

This tells the CLI: use account-a's credentials → call STS AssumeRole with ExternalId →
use the returned temp credentials for all commands run with `--profile crossacct-s3`.

### 7.3 Verify the chain works

```bash
aws sts get-caller-identity --profile crossacct-s3
```

The output must show Account B's account ID and the `CrossAccountS3Role` ARN.
If you see this, the full trust chain is working end to end.

---

## Phase 8 — Testing

### Test 1: Upload via assumed role

```bash
echo "Cross-account test file" > test.txt

aws s3 cp test.txt s3://account-b-crossacct-demo-<random>/test.txt \
  --profile crossacct-s3
```

### Test 2: Verify KMS encryption on the uploaded object

```bash
aws s3api head-object \
  --bucket account-b-crossacct-demo-<random> \
  --key test.txt \
  --profile crossacct-s3
```

Look for these two fields in the output:
- `"ServerSideEncryption": "aws:kms"`
- `"SSEKMSKeyId": "arn:aws:kms:us-east-1:..."` — must match your key ARN

### Test 3: Download the object

```bash
aws s3 cp s3://account-b-crossacct-demo-<random>/test.txt downloaded.txt \
  --profile crossacct-s3

cat downloaded.txt
```

### Test 4: Confirm raw Account A creds are denied (core proof)

```bash
aws s3 ls s3://account-b-crossacct-demo-<random>/ --profile account-a
```

Expected: `An error occurred (AccessDenied)`

This is the key demonstration — Account A's own credentials have zero access.
Only the assumed role grants access.

### Test 5: Upload multiple versions and verify versioning

```bash
echo "Version 1" > test.txt
aws s3 cp test.txt s3://account-b-crossacct-demo-<random>/versioned.txt \
  --profile crossacct-s3

echo "Version 2" > test.txt
aws s3 cp test.txt s3://account-b-crossacct-demo-<random>/versioned.txt \
  --profile crossacct-s3

echo "Version 3" > test.txt
aws s3 cp test.txt s3://account-b-crossacct-demo-<random>/versioned.txt \
  --profile crossacct-s3

aws s3api list-object-versions \
  --bucket account-b-crossacct-demo-<random> \
  --key versioned.txt \
  --profile crossacct-s3
```

You should see three versions, each with a unique `VersionId` and each encrypted with your KMS key.

### Test 6: List bucket

```bash
aws s3 ls s3://account-b-crossacct-demo-<random>/ --profile crossacct-s3
```

### Test 7: Verify bucket security posture

```bash
# Check encryption config
aws s3api get-bucket-encryption \
  --bucket account-b-crossacct-demo-<random> \
  --profile account-b

# Check public access block
aws s3api get-public-access-block \
  --bucket account-b-crossacct-demo-<random> \
  --profile account-b

# Check versioning status
aws s3api get-bucket-versioning \
  --bucket account-b-crossacct-demo-<random> \
  --profile account-b

# Check logging config
aws s3api get-bucket-logging \
  --bucket account-b-crossacct-demo-<random> \
  --profile account-b
```

---

## Phase 9 — Python boto3 Demo Script

Save as `cross_account_s3_demo.py`:

```python
import boto3
import json
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────
ACCOUNT_B_ID   = "<ACCOUNT_B_ID>"
ROLE_ARN       = f"arn:aws:iam::{ACCOUNT_B_ID}:role/CrossAccountS3Role"
EXTERNAL_ID    = "CrossAcctS3Demo"
BUCKET_NAME    = "account-b-crossacct-demo-<random>"
KMS_KEY_ARN    = "<KMS_KEY_ARN>"
REGION         = "us-east-1"
SOURCE_PROFILE = "account-a"


# ── Step 1: Assume cross-account role ────────────────────────────────
def assume_role():
    session = boto3.Session(profile_name=SOURCE_PROFILE)
    sts = session.client("sts", region_name=REGION)

    print("[*] Assuming CrossAccountS3Role in Account B...")
    response = sts.assume_role(
        RoleArn=ROLE_ARN,
        RoleSessionName="CrossAcctS3DemoSession",
        ExternalId=EXTERNAL_ID,
        DurationSeconds=3600
    )
    creds = response["Credentials"]
    print(f"[+] Role assumed successfully.")
    print(f"[+] Session expires at: {creds['Expiration']}")
    return creds


# ── Step 2: Build S3 client from temp credentials ────────────────────
def get_s3_client(creds):
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"]
    )


# ── Step 3: Upload an encrypted object ──────────────────────────────
def upload_object(s3, content, key):
    print(f"\n[*] Uploading '{key}' with SSE-KMS encryption...")
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=content.encode(),
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=KMS_KEY_ARN
    )
    print(f"[+] Upload successful.")


# ── Step 4: Verify encryption metadata on the object ─────────────────
def verify_encryption(s3, key):
    print(f"\n[*] Verifying encryption on '{key}'...")
    response = s3.head_object(Bucket=BUCKET_NAME, Key=key)
    sse       = response.get("ServerSideEncryption", "None")
    kms_id    = response.get("SSEKMSKeyId", "N/A")
    print(f"[+] Encryption algorithm : {sse}")
    print(f"[+] KMS Key ID           : {kms_id}")
    return sse == "aws:kms"


# ── Step 5: Download and read the object ─────────────────────────────
def download_object(s3, key):
    print(f"\n[*] Downloading '{key}'...")
    response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
    content  = response["Body"].read().decode()
    print(f"[+] Content: {content}")
    return content


# ── Step 6: Confirm Account A raw creds are denied ──────────────────
def test_direct_access_denied():
    print(f"\n[*] Testing that Account A raw credentials are denied...")
    try:
        raw_session = boto3.Session(profile_name=SOURCE_PROFILE)
        s3_raw = raw_session.client("s3", region_name=REGION)
        s3_raw.list_objects_v2(Bucket=BUCKET_NAME)
        print("[!] WARNING: Direct access succeeded — check bucket policy.")
    except Exception as e:
        if "AccessDenied" in str(e) or "403" in str(e):
            print("[+] Correctly denied. Account A has no direct access.")
        else:
            print(f"[!] Unexpected error: {e}")


# ── Step 7: Audit bucket security posture ────────────────────────────
def audit_bucket(s3):
    print(f"\n[*] Running security audit on bucket '{BUCKET_NAME}'...")
    report = {
        "bucket": BUCKET_NAME,
        "region": REGION,
        "timestamp": str(datetime.utcnow()) + " UTC"
    }

    # Default encryption
    enc = s3.get_bucket_encryption(Bucket=BUCKET_NAME)
    rules = enc["ServerSideEncryptionConfiguration"]["Rules"]
    rule  = rules[0]["ApplyServerSideEncryptionByDefault"]
    report["default_encryption"] = rule["SSEAlgorithm"]
    report["kms_key"]            = rule.get("KMSMasterKeyID", "N/A")
    report["bucket_key_enabled"] = rules[0].get("BucketKeyEnabled", False)

    # Versioning
    ver = s3.get_bucket_versioning(Bucket=BUCKET_NAME)
    report["versioning"] = ver.get("Status", "Disabled")

    # Public access block
    pub = s3.get_public_access_block(Bucket=BUCKET_NAME)
    cfg = pub["PublicAccessBlockConfiguration"]
    report["block_public_access"] = all(cfg.values())

    # Access logging
    try:
        log = s3.get_bucket_logging(Bucket=BUCKET_NAME)
        report["access_logging"] = "Enabled" if "LoggingEnabled" in log else "Disabled"
    except Exception:
        report["access_logging"] = "Unknown"

    print(json.dumps(report, indent=2))
    return report


# ── Main ─────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Cross-Account S3 + KMS Demo")
    print("  Account A → AssumeRole → Account B")
    print("=" * 60)

    # Assume role and get S3 client
    creds = assume_role()
    s3    = get_s3_client(creds)

    # Upload, verify, download
    test_key = f"demo/test-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"
    upload_object(s3, "Hello from Account A via cross-account role!", test_key)
    encrypted = verify_encryption(s3, test_key)
    download_object(s3, test_key)

    # Security checks
    test_direct_access_denied()
    report = audit_bucket(s3)

    # Summary
    print("\n" + "=" * 60)
    print(f"  KMS encrypted      : {'✅' if encrypted else '❌'}")
    print(f"  Versioning         : {'✅' if report['versioning'] == 'Enabled' else '❌'}")
    print(f"  Block public access: {'✅' if report['block_public_access'] else '❌'}")
    print(f"  Access logging     : {'✅' if report['access_logging'] == 'Enabled' else '⚠️'}")
    print("  Project complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
```

Install dependency and run:

```bash
pip install boto3
python cross_account_s3_demo.py
```

---

## GitHub Repo Structure

```
cross-account-s3-kms/
├── README.md
├── policies/
│   ├── trust-policy.json
│   ├── role-permissions.json
│   ├── bucket-policy.json
│   ├── kms-key-policy.json
│   └── logging-bucket-policy.json
├── scripts/
│   └── cross_account_s3_demo.py
├── docs/
│   └── architecture.png
└── .gitignore
```

`.gitignore`:
```
test.txt
downloaded.txt
*.env
.env
__pycache__/
```

---

## Deletion Guide

Delete in reverse order — policies before roles before resources.

### Step 1: Empty the main bucket

Versioning requires explicit deletion of all versions and delete markers.

```bash
# Delete all object versions
aws s3api list-object-versions \
  --bucket account-b-crossacct-demo-<random> \
  --query 'Versions[].{Key:Key,VersionId:VersionId}' \
  --output json --profile account-b | \
python3 -c "
import sys, json, subprocess
versions = json.load(sys.stdin)
for v in versions:
    subprocess.run([
        'aws', 's3api', 'delete-object',
        '--bucket', 'account-b-crossacct-demo-<random>',
        '--key', v['Key'],
        '--version-id', v['VersionId'],
        '--profile', 'account-b'
    ])
    print(f'Deleted version: {v[\"Key\"]} @ {v[\"VersionId\"]}')
"

# Delete all delete markers
aws s3api list-object-versions \
  --bucket account-b-crossacct-demo-<random> \
  --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' \
  --output json --profile account-b | \
python3 -c "
import sys, json, subprocess
markers = json.load(sys.stdin)
for m in markers:
    subprocess.run([
        'aws', 's3api', 'delete-object',
        '--bucket', 'account-b-crossacct-demo-<random>',
        '--key', m['Key'],
        '--version-id', m['VersionId'],
        '--profile', 'account-b'
    ])
    print(f'Deleted marker: {m[\"Key\"]}')
"
```

### Step 2: Delete both S3 buckets

```bash
# Delete main bucket (now empty)
aws s3 rb s3://account-b-crossacct-demo-<random> --profile account-b

# Clear and delete logging bucket
aws s3 rm s3://account-b-crossacct-logs-<random> --recursive --profile account-b
aws s3 rb s3://account-b-crossacct-logs-<random> --profile account-b
```

### Step 3: Delete IAM role in Account B

Inline policies must be deleted before the role itself.

```bash
aws iam delete-role-policy \
  --role-name CrossAccountS3Role \
  --policy-name S3KMSAccessPolicy \
  --profile account-b

aws iam delete-role \
  --role-name CrossAccountS3Role \
  --profile account-b
```

### Step 4: Delete IAM user policy in Account A

```bash
aws iam delete-user-policy \
  --user-name <your-iam-username-in-account-a> \
  --policy-name AssumeAccountBRole \
  --profile account-a
```

### Step 5: Schedule KMS key deletion

AWS enforces a minimum 7-day waiting period before a key is permanently deleted.
The key is disabled immediately — it cannot encrypt or decrypt during the waiting period.

```bash
# Delete alias first
aws kms delete-alias \
  --alias-name alias/s3-crossaccount-key \
  --region us-east-1 \
  --profile account-b

# Schedule key deletion (7-day minimum)
aws kms schedule-key-deletion \
  --key-id <KeyId> \
  --pending-window-in-days 7 \
  --region us-east-1 \
  --profile account-b
```

### Step 6: Remove chained CLI profile

Delete the `[profile crossacct-s3]` block from `~/.aws/config`.

### Step 7: Verify everything is gone

```bash
# Buckets gone
aws s3 ls --profile account-b | grep crossacct

# Role gone — expects NoSuchEntity error
aws iam get-role --role-name CrossAccountS3Role --profile account-b

# Account A policy gone — expects NoSuchEntity error
aws iam get-user-policy \
  --user-name <your-iam-username-in-account-a> \
  --policy-name AssumeAccountBRole \
  --profile account-a

# KMS key in PendingDeletion state
aws kms describe-key \
  --key-id <KeyId> \
  --region us-east-1 \
  --profile account-b
# Expected: "KeyState": "PendingDeletion"
```

---

## Quick Reference — All Corrections Applied

| Issue from earlier review | Status |
|--------------------------|--------|
| `--create-bucket-configuration` removed for us-east-1 | ✅ Fixed |
| Build order: S3 → Role → KMS (not KMS first) | ✅ Fixed |
| Logging bucket gets `logging.s3.amazonaws.com` policy | ✅ Fixed |
| All regions updated to us-east-1 | ✅ Fixed |
| Python script REGION updated to us-east-1 | ✅ Fixed |
| Role policy updated after KMS ARN is known | ✅ Fixed |
