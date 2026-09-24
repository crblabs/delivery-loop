# adapters

One directory per harness. An adapter is the only place a harness name may
appear.

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
