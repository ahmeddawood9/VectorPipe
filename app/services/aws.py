"""One place that builds boto3 clients, so S3 and SQS authenticate and retry identically."""

from __future__ import annotations


def create_client(service: str, *, region: str | None, profile: str | None, read_timeout: float):
    """Return a boto3 client for ``service``.

    With ``profile`` set, boto3 resolves credentials from that named profile in ``~/.aws/config``. A profile
    that has ``role_arn`` + ``source_profile`` makes boto3 assume the role itself and refresh the temporary
    credentials before they expire, so the app needs no STS code. Without a profile, boto3 uses its normal
    chain (env vars, SSO, instance/task role).
    """
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client(
        service,
        config=Config(retries={"mode": "standard", "max_attempts": 5}, connect_timeout=5, read_timeout=read_timeout),
    )
