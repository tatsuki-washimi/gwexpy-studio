# Security policy

## Supported trial scope

The first wheel trial is limited to the platforms and build IDs stated by its
GitHub prerelease.

An unpublished source checkout, an arbitrary dependency set, or an unreviewed
I/O backend is not a supported trial artifact.

## Reporting a vulnerability

Do not post a suspected vulnerability, private project, local path, dataset, or
credential in a public issue.

Use the repository's private security-advisory reporting channel when it is
enabled.

If that channel is unavailable, contact the maintainers through the contact
method stated in the repository profile and include only the minimum
reproduction details needed to establish impact.

## Data handling

Studio treats user data paths and dataset metadata as sensitive operational
context.

Path-free diagnostics are safe to copy into an issue.

Logs and project files can contain paths or metadata and should be shared only
when a maintainer requests them through an appropriate private channel.
