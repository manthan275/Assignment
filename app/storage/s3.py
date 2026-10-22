"""S3-compatible storage boundary for local development and AWS deployments.

An object only becomes visible once it is fully written, so its existence marks a
complete export that a resumed run can reuse.
"""
import os
from pathlib import Path
import shutil


class S3Store:
    """Local filesystem stand-in used by tests and local development."""
    def __init__(self, root="./runtime/s3"):
        self.root = Path(root)

    @staticmethod
    def _safe_key(key: str) -> Path:
        key_path = Path(key)
        if key_path.is_absolute() or ".." in key_path.parts:
            raise ValueError("S3 object key must be a relative path without '..'")
        return key_path

    def put_file(self, source, key):
        key_path = self._safe_key(key)
        destination = self.root / key_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        shutil.copyfile(source, partial)
        os.replace(partial, destination)
        return str(destination)

    def get_file(self, key):
        return self.root / self._safe_key(key)

    def exists(self, key) -> bool:
        return (self.root / self._safe_key(key)).is_file()

    def download_file(self, key, destination) -> None:
        shutil.copyfile(self.root / self._safe_key(key), destination)


class AwsS3Store:
    """AWS S3 implementation; upload_file streams from disk rather than loading CSV into RAM."""
    def __init__(self, bucket: str, *, s3_client=None):
        if not bucket:
            raise ValueError("S3_BUCKET is required for AWS storage")
        if s3_client is None:
            import boto3
            s3_client = boto3.client("s3")
        self.bucket = bucket
        self.client = s3_client

    def put_file(self, source, key):
        S3Store._safe_key(key)
        self.client.upload_file(str(source), self.bucket, key)
        return f"s3://{self.bucket}/{key}"

    def get_file(self, key):
        """Return an S3 URI; callers should download it to task-local storage first."""
        S3Store._safe_key(key)
        return f"s3://{self.bucket}/{key}"

    def exists(self, key) -> bool:
        from botocore.exceptions import ClientError

        S3Store._safe_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def download_file(self, key, destination) -> None:
        S3Store._safe_key(key)
        self.client.download_file(self.bucket, key, str(destination))
