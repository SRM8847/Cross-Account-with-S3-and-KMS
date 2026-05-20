# Cross-Account S3 + KMS Encryption

A hands-on AWS security project demonstrating **cross-account S3 access using STS AssumeRole and KMS encryption**, following real-world least-privilege and defense-in-depth principles.

---

## What This Project Demonstrates

| Concept | Implementation |
|---------|---------------|
| Cross-account IAM trust | Account A assumes a role in Account B via STS |
| Least privilege | Role grants only S3 + KMS actions needed, nothing else |
| Confused deputy protection | `ExternalId` condition on the trust policy |
| Encryption at rest | SSE-KMS with a Customer Managed Key (CMK) |
| Encryption in transit | Bucket policy denies all non-SSL requests |
| Unencrypted upload prevention | Bucket policy denies PutObject without SSE-KMS header |
| Object versioning | S3 versioning enabled for all objects |
| Access logging | S3 server access logs shipped to a separate logging bucket |
| Security posture auditing | boto3 script audits all bucket security controls |

---

## Architecture

```
Account A                                  Account B
─────────────────────────────              ──────────────────────────────────────────
IAM User: account-a-user                   IAM Role: CrossAccountS3Role
  └─ Inline policy:                          └─ Trust policy:
       sts:AssumeRole ──────────────────────►    Principal: Account A
       on CrossAccountS3Role                     Condition: ExternalId = CrossAcctS3Demo
                                               └─ Permission policy:
                                                    s3:GetObject, PutObject, ListBucket...
                                                    kms:GenerateDataKey, Decrypt, DescribeKey

                                           S3 Bucket: account-b-crossacct-demo-<random>
                                             └─ Default encryption: SSE-KMS (CMK)
                                             └─ BucketKeyEnabled: true
                                             └─ Versioning: Enabled
                                             └─ Block Public Access: all enabled
                                             └─ Bucket policy:
                                                  Deny aws:SecureTransport = false
                                                  Deny PutObject without SSE-KMS header
                                             └─ Access logging → account-b-crossacct-logs-<random>

                                           KMS CMK: alias/s3-crossaccount-key
                                             └─ Key policy:
                                                  Account B root: kms:*
                                                  CrossAccountS3Role: GenerateDataKey, Decrypt, DescribeKey
```

**Request flow:**
```
Account A CLI
  → STS AssumeRole (with ExternalId)
  → Temporary credentials (1h TTL)
  → S3 PutObject / GetObject
  → KMS GenerateDataKey / Decrypt (transparent, handled by S3)
  → Object stored encrypted in Account B bucket
```

---

## Repo Structure

```
cross-account-s3-kms/
├── README.md
├── .gitignore
├── policies/
│   ├── trust-policy.json            # Account B role trust — allows Account A to assume
│   ├── role-permissions.json        # Account B role permissions — S3 + KMS actions
│   ├── assume-role-policy.json      # Account A user policy — sts:AssumeRole only
│   ├── bucket-policy.json           # Main bucket — DenyNonSSL + DenyUnencrypted
│   ├── kms-key-policy.json          # KMS CMK — root + CrossAccountS3Role access
│   └── logging-bucket-policy.json   # Logging bucket — S3 log delivery service principal
├── scripts/
│   └── cross_account_s3_demo.py     # boto3 demo: assume role, upload, verify, audit
└── docs/
    └── architecture.png             # Architecture diagram (add your own screenshot)
```

---

## Prerequisites

- Two AWS accounts with CLI profiles configured (`account-a`, `account-b`)
- AWS CLI v2 installed
- Python 3.8+ and `boto3` installed (`pip install boto3`)
- IAM user in Account A with permissions to call `sts:AssumeRole`

Verify both profiles:
```bash
aws sts get-caller-identity --profile account-a
aws sts get-caller-identity --profile account-b
```

---

## Setup — Build Order

> **Order matters.** The IAM role must exist before the KMS key policy references it.
> S3 buckets must exist before logging and encryption are configured.

```
Phase 1 → S3 Buckets (Account B)
Phase 2 → IAM Role (Account B)        ← must exist before KMS key policy references it
Phase 3 → KMS Key (Account B)         ← now key policy can safely name the role
Phase 4 → S3 Encryption + Policies (Account B)
Phase 5 → Update Role Policy with real KMS ARN (Account B)
Phase 6 → AssumeRole Permission (Account A)
Phase 7 → CLI Profile Setup
```

See **[`cross-account-s3-kms-guide.md`](./cross-account-s3-kms-guide.md)** for the complete step-by-step setup with every CLI command.

---

## Phase 1 — Account B: S3 Buckets

> **us-east-1 note:** Do NOT use `--create-bucket-configuration LocationConstraint` for us-east-1.
> AWS throws `InvalidLocationConstraint` if you pass it for the default region.

```bash
# Main bucket
aws s3api create-bucket \
  --bucket account-b-crossacct-demo-<random> \
  --region us-east-1 \
  --profile account-b

# Logging bucket
aws s3api create-bucket \
  --bucket account-b-crossacct-logs-<random> \
  --region us-east-1 \
  --profile account-b

# Apply logging bucket policy (allows S3 log delivery service to write)
aws s3api put-bucket-policy \
  --bucket account-b-crossacct-logs-<random> \
  --policy file://policies/logging-bucket-policy.json \
  --profile account-b

# Block public access on both
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

# Enable versioning
aws s3api put-bucket-versioning \
  --bucket account-b-crossacct-demo-<random> \
  --versioning-configuration Status=Enabled \
  --profile account-b

# Enable access logging
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

## Phase 2 — Account B: IAM Role

```bash
# Create role with trust policy (replace <ACCOUNT_A_ID> in file first)
aws iam create-role \
  --role-name CrossAccountS3Role \
  --assume-role-policy-document file://policies/trust-policy.json \
  --description "Allows Account A to access Account B S3 bucket and KMS key" \
  --profile account-b

# Attach permission policy (update bucket name in file first; leave KMS ARN as placeholder)
aws iam put-role-policy \
  --role-name CrossAccountS3Role \
  --policy-name S3KMSAccessPolicy \
  --policy-document file://policies/role-permissions.json \
  --profile account-b
```

**Save the `RoleArn` from the create-role output.**

---

## Phase 3 — Account B: KMS Key

```bash
# Create CMK
aws kms create-key \
  --description "Cross-account S3 encryption key" \
  --key-usage ENCRYPT_DECRYPT \
  --region us-east-1 \
  --profile account-b

# Create alias
aws kms create-alias \
  --alias-name alias/s3-crossaccount-key \
  --target-key-id <KeyId> \
  --region us-east-1 \
  --profile account-b

# Apply key policy (replace <ACCOUNT_B_ID> in file first)
aws kms put-key-policy \
  --key-id <KeyId> \
  --policy-name default \
  --policy file://policies/kms-key-policy.json \
  --region us-east-1 \
  --profile account-b
```

**Copy the `KeyId` and `KeyArn` from the create-key output.**

---

## Phase 4 — Account B: Bucket Encryption + Bucket Policy

```bash
# Enable SSE-KMS default encryption (replace <KMS_KEY_ARN>)
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

# Apply bucket policy (update bucket name in file first)
aws s3api put-bucket-policy \
  --bucket account-b-crossacct-demo-<random> \
  --policy file://policies/bucket-policy.json \
  --profile account-b
```

---

## Phase 5 — Account B: Update Role Policy with Real KMS ARN

Replace `PLACEHOLDER_UPDATE_AFTER_PHASE_3` in `policies/role-permissions.json` with the actual KMS ARN, then re-apply:

```bash
aws iam put-role-policy \
  --role-name CrossAccountS3Role \
  --policy-name S3KMSAccessPolicy \
  --policy-document file://policies/role-permissions.json \
  --profile account-b
```

---

## Phase 6 — Account A: Grant AssumeRole Permission

```bash
# Replace <ACCOUNT_B_ID> in policies/assume-role-policy.json first
aws iam put-user-policy \
  --user-name <your-iam-username-in-account-a> \
  --policy-name AssumeAccountBRole \
  --policy-document file://policies/assume-role-policy.json \
  --profile account-a
```

---

## Phase 7 — CLI Profile Setup

Add to `~/.aws/config`:

```ini
[profile crossacct-s3]
role_arn     = arn:aws:iam::<ACCOUNT_B_ID>:role/CrossAccountS3Role
source_profile = account-a
external_id  = CrossAcctS3Demo
region       = us-east-1
```

Verify the full trust chain:
```bash
aws sts get-caller-identity --profile crossacct-s3
# Must show Account B ID and CrossAccountS3Role ARN
```

---

## Testing

```bash
# 1. Upload via assumed role
echo "Cross-account test file" > test.txt
aws s3 cp test.txt s3://account-b-crossacct-demo-<random>/test.txt --profile crossacct-s3

# 2. Verify KMS encryption (look for "ServerSideEncryption": "aws:kms")
aws s3api head-object \
  --bucket account-b-crossacct-demo-<random> \
  --key test.txt \
  --profile crossacct-s3

# 3. Download
aws s3 cp s3://account-b-crossacct-demo-<random>/test.txt downloaded.txt --profile crossacct-s3

# 4. Confirm Account A raw creds are DENIED (key proof)
aws s3 ls s3://account-b-crossacct-demo-<random>/ --profile account-a
# Expected: AccessDenied

# 5. Run the boto3 demo script
python scripts/cross_account_s3_demo.py
```

---

## Running the Demo Script

Update the config section at the top of `scripts/cross_account_s3_demo.py`:

```python
ACCOUNT_B_ID = "<ACCOUNT_B_ID>"
BUCKET_NAME  = "account-b-crossacct-demo-<random>"
KMS_KEY_ARN  = "<KMS_KEY_ARN>"
```

Then run:
```bash
pip install boto3
python scripts/cross_account_s3_demo.py
```

Expected output:
```
============================================================
  Cross-Account S3 + KMS Demo
  Account A → AssumeRole → Account B
============================================================
[*] Assuming CrossAccountS3Role in Account B...
[+] Role assumed successfully.
[+] Session expires at: 2024-xx-xx xx:xx:xx+00:00

[*] Uploading 'demo/test-20240101-120000.txt' with SSE-KMS encryption...
[+] Upload successful.

[*] Verifying encryption on 'demo/test-20240101-120000.txt'...
[+] Encryption algorithm : aws:kms
[+] KMS Key ID           : arn:aws:kms:us-east-1:xxxxxxxxxxxx:key/...

[*] Downloading 'demo/test-20240101-120000.txt'...
[+] Content: Hello from Account A via cross-account role!

[*] Testing that Account A raw credentials are denied...
[+] Correctly denied. Account A has no direct access.

[*] Running security audit on bucket 'account-b-crossacct-demo-<random>'...
{
  "bucket": "account-b-crossacct-demo-<random>",
  "region": "us-east-1",
  "default_encryption": "aws:kms",
  "versioning": "Enabled",
  "block_public_access": true,
  "access_logging": "Enabled"
}

============================================================
  KMS encrypted      : ✅
  Versioning         : ✅
  Block public access: ✅
  Access logging     : ✅
  Project complete.
============================================================
```

---

## Cleanup / Deletion

Delete in reverse order — policies before roles before resources.

```bash
# 1. Empty main bucket (versioning requires explicit version deletion)
aws s3api list-object-versions \
  --bucket account-b-crossacct-demo-<random> \
  --query 'Versions[].{Key:Key,VersionId:VersionId}' \
  --output json --profile account-b | \
python3 -c "
import sys, json, subprocess
for v in json.load(sys.stdin):
    subprocess.run(['aws','s3api','delete-object',
        '--bucket','account-b-crossacct-demo-<random>',
        '--key',v['Key'],'--version-id',v['VersionId'],
        '--profile','account-b'])
    print(f'Deleted: {v[\"Key\"]} @ {v[\"VersionId\"]}')
"

aws s3api list-object-versions \
  --bucket account-b-crossacct-demo-<random> \
  --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' \
  --output json --profile account-b | \
python3 -c "
import sys, json, subprocess
for m in json.load(sys.stdin):
    subprocess.run(['aws','s3api','delete-object',
        '--bucket','account-b-crossacct-demo-<random>',
        '--key',m['Key'],'--version-id',m['VersionId'],
        '--profile','account-b'])
"

# 2. Delete both S3 buckets
aws s3 rb s3://account-b-crossacct-demo-<random> --profile account-b
aws s3 rm s3://account-b-crossacct-logs-<random> --recursive --profile account-b
aws s3 rb s3://account-b-crossacct-logs-<random> --profile account-b

# 3. Delete IAM role (Account B) — inline policy first
aws iam delete-role-policy \
  --role-name CrossAccountS3Role --policy-name S3KMSAccessPolicy --profile account-b
aws iam delete-role --role-name CrossAccountS3Role --profile account-b

# 4. Delete IAM user policy (Account A)
aws iam delete-user-policy \
  --user-name <your-iam-username-in-account-a> \
  --policy-name AssumeAccountBRole --profile account-a

# 5. Schedule KMS key deletion (7-day minimum wait)
aws kms delete-alias \
  --alias-name alias/s3-crossaccount-key --region us-east-1 --profile account-b
aws kms schedule-key-deletion \
  --key-id <KeyId> --pending-window-in-days 7 --region us-east-1 --profile account-b

# 6. Remove [profile crossacct-s3] from ~/.aws/config
```

---

## Security Controls Summary

| Control | Mechanism | Where |
|---------|-----------|-------|
| Cross-account boundary | STS AssumeRole + trust policy | IAM |
| Confused deputy prevention | `sts:ExternalId` condition | Trust policy |
| Encryption at rest | SSE-KMS with CMK | S3 + KMS |
| Encryption in transit | `aws:SecureTransport` deny | Bucket policy |
| No unencrypted uploads | SSE header enforcement deny | Bucket policy |
| No public access | Block Public Access (all 4 settings) | S3 |
| Object recovery | Versioning enabled | S3 |
| Activity visibility | Server access logging | S3 |
| Least privilege KMS | CMK grants only GenerateDataKey/Decrypt/DescribeKey | KMS key policy |

---

## Key Concepts for Interviews

**Why ExternalId?**
Without it, any AWS account could trick Account B into assuming the role on their behalf (confused deputy problem). ExternalId acts as a shared secret between the two accounts.

**Why BucketKeyEnabled?**
Without it, every S3 operation makes a KMS API call. With it, S3 generates a bucket-level data key and reuses it, reducing KMS calls (and cost) dramatically.

**Why can't Account A access the bucket directly?**
The bucket has no policy granting Account A access. Only `CrossAccountS3Role` (which lives in Account B) has S3 permissions. Account A's raw credentials never touch the bucket — only the assumed role's temporary credentials do.

**Why is the role in Account B, not Account A?**
Because the resources (S3, KMS) are in Account B. Keeping the role in the same account as the resources means standard same-account IAM evaluation applies — no cross-account policy on every individual resource.

---

## Tech Stack

- **AWS S3** — object storage with versioning and server-side encryption
- **AWS KMS** — customer managed key for envelope encryption
- **AWS IAM / STS** — cross-account role assumption with ExternalId
- **Python 3 + boto3** — programmatic demo and security audit
- **AWS CLI v2** — infrastructure setup

---

## Related Projects

- [Least-Privilege IAM Lab](https://github.com/SRM8847/Least-Privilege-IAM) — direct RBAC, forced role assumption, session policy narrowing
