# templates

Files a host repository copies and fills in. A template is an example, never a
live configuration: nothing in this directory is read at run time.

## What belongs here

- `loop.toml`: the per-repo configuration seam. Every value the loop needs that
  differs from one host repository to the next, with a comment saying what it
  is and an empty value for the host to fill in.
- Any further template a host repository would copy, if one is added later.

## What does not belong here

- A filled-in configuration for any real repository. Those live in the host
  repository, not here.
- Secrets, tokens or credentials of any kind.
- Code.

## Note

The key set in `loop.toml` is the current shape, not a frozen schema. Where a
key's exact form is not yet decided, the file says "unsettled" in the comment.
