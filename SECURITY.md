# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, notify AWS Security
through the [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or at [aws-security@amazon.com](mailto:aws-security@amazon.com).
Do **not** create a public GitHub issue for a security vulnerability.

## Scope and Intent

This repository is **sample code**. It shows how AWS DevOps Agent can delegate
read-only, engine-internal database diagnostics to specialist agents on Amazon
Bedrock AgentCore Runtime over the A2A protocol. It is provided for educational
and proof-of-concept use and is **not** intended for production use as-is.

Before you deploy any part of it, review and harden it for your environment,
including:

- the IAM roles and runtime resource policies in `infra/`, scoped to your
  accounts, Agent Space and runtimes
- the network path and TLS settings between each specialist and its database
- the database principal the specialist uses, which should be read-only
- logging, retention and alarms for the adapters and specialists
- your organization's security, regulatory and compliance requirements
