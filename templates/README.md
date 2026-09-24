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

`loop.toml` now holds two kinds of key. The ones `core/config.py` reads carry
the value the loop uses when the key is absent, so deleting a key is the same
as writing its default, and the template as it ships reproduces the built-in
defaults exactly. The ones marked "not read yet" are the shape the loop is
heading for: nothing reads them, and an unread key is ignored, not refused.
