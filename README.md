# indra-memory

Long-term memory for AI agents, in SQLite, outside the context window.

## The problem

Context-bound memory degrades with length. The model physically cannot see the
start of its own conversation, so everything "remembered" is really just a
file. Facts, decisions and agreements live in a window and are lost.

This is built the other way around: facts live **outside** the context in
SQLite with a full-text index, and only a relevance-ranked sample enters the
prompt. The length of the conversation stops being the limit on memory.

## Properties a flat JSON file does not have

- **Full-text search** — recall by relevance, not by key
- **Supersede, not overwrite** — a corrected fact does not erase the old one;
  it replaces it while both stay retrievable
- **Links between facts** — related facts are neighbours, not strings
- **Token-budgeted packing** — exactly as much context as fits, no more
- **Importance** — not every fact deserves a slot in the prompt

## Try it

```
curl -X POST https://rta.lat/memory/remember \
  -H 'Content-Type: application/json' \
  -d '{"text":"the user prefers Russian","tags":["pref"]}'

curl 'https://rta.lat/memory/recall?q=language'
curl  https://rta.lat/memory/stats
curl -X POST https://rta.lat/memory/forget -d '{"id":1}'
```

## The measurement behind it

The design follows a measurement, not an intuition. Across five independent
model runs, loss of facts between sessions came up as the top pain every time
— 5 out of 5. The reason is structural, not a matter of model quality.

That measurement is in the repository as well; the point is that the decision
to build this had evidence behind it.

## Public repo

<https://github.com/mrpkk/indra-memory>

Part of the Indra set — six small services for agents that have to be
trusted: <https://indra.rta.lat>
