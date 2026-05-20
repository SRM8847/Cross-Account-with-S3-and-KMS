# docs/

Place your architecture diagram here as `architecture.png`.

You can screenshot the ASCII architecture from the README, or draw it using:
- https://draw.io (free, export as PNG)
- https://excalidraw.com (free, hand-drawn style)
- AWS Architecture Diagram templates in draw.io

Recommended diagram elements:
- Account A box (IAM User → AssumeRole arrow)
- Account B box (IAM Role, S3 Bucket, KMS Key)
- Arrow labeled "STS AssumeRole + ExternalId"
- Arrow labeled "Temp credentials (1h TTL)"
- Lock icon on S3 bucket (SSE-KMS)
