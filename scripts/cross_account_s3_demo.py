import boto3
import json
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
# Replace all placeholders below before running
ACCOUNT_B_ID   = "<ACCOUNT_B_ID>"
ROLE_ARN       = f"arn:aws:iam::{ACCOUNT_B_ID}:role/CrossAccountS3Role"
EXTERNAL_ID    = "CrossAcctS3Demo"
BUCKET_NAME    = "account-b-crossacct-demo-<random>"
KMS_KEY_ARN    = "<KMS_KEY_ARN>"
REGION         = "us-east-1"
SOURCE_PROFILE = "account-a"


# ── Step 1: Assume cross-account role ───────────────────────────────────────
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


# ── Step 2: Build S3 client from temporary credentials ──────────────────────
def get_s3_client(creds):
    return boto3.client(
        "s3",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"]
    )


# ── Step 3: Upload an object with SSE-KMS encryption ────────────────────────
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


# ── Step 4: Verify encryption metadata on the uploaded object ───────────────
def verify_encryption(s3, key):
    print(f"\n[*] Verifying encryption on '{key}'...")
    response = s3.head_object(Bucket=BUCKET_NAME, Key=key)
    sse    = response.get("ServerSideEncryption", "None")
    kms_id = response.get("SSEKMSKeyId", "N/A")
    print(f"[+] Encryption algorithm : {sse}")
    print(f"[+] KMS Key ID           : {kms_id}")
    return sse == "aws:kms"


# ── Step 5: Download and read the object ────────────────────────────────────
def download_object(s3, key):
    print(f"\n[*] Downloading '{key}'...")
    response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
    content  = response["Body"].read().decode()
    print(f"[+] Content: {content}")
    return content


# ── Step 6: Confirm Account A raw credentials are denied ────────────────────
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


# ── Step 7: Audit bucket security posture ───────────────────────────────────
def audit_bucket(s3):
    print(f"\n[*] Running security audit on bucket '{BUCKET_NAME}'...")
    report = {
        "bucket":    BUCKET_NAME,
        "region":    REGION,
        "timestamp": str(datetime.utcnow()) + " UTC"
    }

    # Default encryption
    enc   = s3.get_bucket_encryption(Bucket=BUCKET_NAME)
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


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Cross-Account S3 + KMS Demo")
    print("  Account A -> AssumeRole -> Account B")
    print("=" * 60)

    # Assume role and build S3 client from temp creds
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
    print(f"  KMS encrypted      : {'YES' if encrypted else 'NO'}")
    print(f"  Versioning         : {'YES' if report['versioning'] == 'Enabled' else 'NO'}")
    print(f"  Block public access: {'YES' if report['block_public_access'] else 'NO'}")
    print(f"  Access logging     : {'YES' if report['access_logging'] == 'Enabled' else 'CHECK'}")
    print("  Project complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
