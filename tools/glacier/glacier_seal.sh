#!/usr/bin/env bash
# Seal a finished Glacier archive bucket so nothing can change it.
#
# Usage (on home-server, after `aws login` as the account root):
#   tools/glacier/glacier_seal.sh backup-2026-07-12           # seal one bucket
#   tools/glacier/glacier_seal.sh --check backup-2026-07-12   # only show whether it is sealed
#
# Why (George, 2026-11-12): "make the glacier keys write only so ransomware couldn't modify
# existing glacier backups". Two layers:
#   1. The rclone "glacier:" key on the PC is limited by writer-policy.json: it can create
#      backup-* buckets, list them and upload, but it can't delete anything or change a
#      bucket's policy, lifecycle, versioning or ACLs.
#   2. Uploading can still overwrite an object with the same name. So once a yearly run is
#      verified complete, this script puts a bucket policy on that bucket that denies every
#      write, delete and reconfiguration to EVERY principal - the glacier key, any other IAM
#      key in the account (the PC's AWS CLI key too), anything. Reading and Deep Archive
#      restores still work.
#
# Only the root user can undo a seal: AWS always lets root delete a bucket policy, even one
# that denies it (`aws s3api delete-bucket-policy --bucket <name>`). Root signs in through
# the browser, so its access never sits on the PC for ransomware to find.
set -euo pipefail

check_only=0
if [[ "${1:-}" == "--check" ]]; then check_only=1; shift; fi
bucket="${1:?usage: glacier_seal.sh [--check] <bucket>}"
[[ "$bucket" == backup-* ]] || { echo "refusing: $bucket is not a backup-* bucket" >&2; exit 2; }

current=$(aws s3api get-bucket-policy --bucket "$bucket" --query Policy --output text 2>/dev/null || true)
if [[ "$current" == *'"SealedArchive"'* ]]; then
  echo "[seal] $bucket is sealed"
  exit 0
fi
if (( check_only )); then
  echo "[seal] $bucket is NOT sealed"
  exit 1
fi
if [[ -n "$current" && "$current" != "None" ]]; then
  echo "[seal] $bucket already has a different bucket policy; not replacing it:" >&2
  echo "$current" >&2
  exit 1
fi

objects=$(aws s3api list-objects-v2 --bucket "$bucket" --query 'length(Contents[])' --output text)
# Block public access on the bucket while that can still be set (the seal denies changing it).
aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
echo "[seal] $bucket holds $objects object(s)"

policy=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SealedArchive",
      "Effect": "Deny",
      "Principal": "*",
      "Action": [
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:PutObjectAcl",
        "s3:PutObjectVersionAcl",
        "s3:PutObjectTagging",
        "s3:PutObjectRetention",
        "s3:PutObjectLegalHold",
        "s3:BypassGovernanceRetention",
        "s3:DeleteBucket",
        "s3:PutBucketPolicy",
        "s3:DeleteBucketPolicy",
        "s3:PutLifecycleConfiguration",
        "s3:PutBucketVersioning",
        "s3:PutBucketAcl",
        "s3:PutBucketOwnershipControls",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutReplicationConfiguration",
        "s3:PutBucketObjectLockConfiguration"
      ],
      "Resource": ["arn:aws:s3:::$bucket", "arn:aws:s3:::$bucket/*"]
    }
  ]
}
JSON
)
aws s3api put-bucket-policy --bucket "$bucket" --policy "$policy"
echo "[seal] $bucket sealed"
