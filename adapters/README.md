# adapters

One directory per harness, and one per issue tracker. An adapter is the only
place a harness name may appear.

A tracker adapter, such as `linear/`, implements the `Tracker` protocol in
`core/issue_draft.py` for `loop-issue`: it resolves names to ids, writes an
issue, and lists the active issues and projects the audit counts. It holds no
lint rule and no audit rule; those are `core/`.

An adapter translates in both directions:

- **Inbound.** It takes the harness's raw event payloads and turns them into
  core events.
- **Outbound.** It takes core's verdicts and turns them into whatever the
  harness expects back: an exit code, a JSON response, a blocked turn, a
  reinjected message.

An adapter holds no loop logic. It decides nothing about stages, pauses or
guards. It parses, it maps, it returns. If an adapter starts making a decision,
that decision belongs in `core/`.

The interface an adapter must satisfy is in `docs/adapter-contract.md`.
