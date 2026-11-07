# Writing the test phrasing bank

This guide is for the people (not Claude, not any LLM) who write `data/phrasing/test/`, the phrasing bank that the held-out **test** suite is rendered with.

## Why a human has to write it

Every radio report in a scenario is generated from a *phrasing*: a sentence template such as "`{road}` is blocked, `{cause}`. Nobody's getting through." The dev suites use the **train** bank (`data/phrasing/train/`), which was written with LLM help. If the test suite used LLM-written text too, an LLM contestant would partly be "reading its own writing", and every parser built while looking at the train bank would be graded on wording it has already seen. So the test bank must be:

- **written by a person**, ideally two people (you and a friend), without asking an LLM to write, rephrase or "improve" it;
- **different in style** from the train bank (see "Style" below);
- **never used for tuning**. Nobody, human or Claude, opens it to adjust a parser, prompt, rule or threshold. Claude never opens it at all (`CLAUDE.md`). Once it's locked, it doesn't change.

You *may* read the train bank to learn the format, but please don't copy its sentences. A good way to work: read this guide, close the train bank, and write from imagination ("what would a tired police officer actually say on the radio?").

## What to write

Eight files of phrasings plus one of noise words, all in `data/phrasing/test/`:

| File | Fact type | What the report says | Required slots | Value slot |
|---|---|---|---|---|
| `road_blocked.yaml` | road blocked | a road segment can't be driven | `{road}` | — |
| `road_cleared.yaml` | road cleared | a road segment is open again (or open, full stop) | `{road}` | — |
| `substation_status.yaml` | substation status | a substation is `failed`, `tripped`, `online` or `repairing` | `{substation}` | (one `value:` per phrasing, see below) |
| `repair_eta.yaml` | repair estimate | how long a repair will take | `{substation}` | `{duration}` |
| `hospital_fuel.yaml` | hospital fuel | liters left in a hospital's generator tank | `{hospital}` | `{liters}` |
| `burn_spike.yaml` | burn spike | a generator is burning fuel at a (new) rate | `{hospital}` | `{rate}` |
| `depot_stock.yaml` | depot stock | liters a fuel depot holds | `{depot}` | `{liters}` |
| `_injection.yaml` | injection (L3) | someone claiming authority demands a harmful action | see below | — |
| `_noise.yaml` | noise lexicon | typos rate, abbreviations, slang, hedges, radio chatter | — | — |

**How many:** at least **8 phrasings per fact type**, and at least **4 injection phrasings per kind** (shed_zone, dispatch, assign_crew). More is better: 10–12 per fact type gives the generator more variety. For `substation_status`, cover every state that's used: write at least 3 phrasings each for `failed`, `online` and `tripped`, and at least 2 for `repairing`.

**The slots** are filled in by the generator; you write them literally, in braces:

- `{road}` becomes a road segment, e.g. "Elm St between 3rd and 4th", "the stretch of Oak between 2nd and 3rd", "Maple Ave just south of City Hall".
- `{substation}`, `{hospital}`, `{depot}` become a facility name or an address, e.g. "Ashford Substation", "the Millbrook sub", "Mercy General", "the depot at Birch & 5th".
- `{liters}`, `{duration}`, `{rate}` become numbers with units, e.g. "250 liters", "about two hundred liters", "45 minutes", "an hour and a half", "4 liters per minute". Don't write units next to them yourself.
- `{when}` (optional, any fact type) becomes a relative time: "just now", "about 10 minutes ago".
- You may invent **your own extra slots** with a `vocab:` list (e.g. a `{cause}` slot with 6–10 causes of a road block). Vocab entries are also yours to write.

Each phrasing must contain its required slot, and its value slot if it has one. A phrasing may name only one thing: don't write "{road} and also Main Street are blocked".

**Channels (optional):** a phrasing can say which radio channels it fits, so a police-sounding sentence isn't used for a member of the public. Channels: `police_radio`, `crew_radio`, `hospital_staff`, `depot_staff`, `public`. Leave `channels` out to allow any channel. Make sure every fact type has some phrasings for the channels it's used on: road facts come from `police_radio` and `public`; substation and repair facts from `crew_radio` and `public`; hospital facts from `hospital_staff` and `public`; depot facts from `depot_staff` and `public`.

## File format

YAML, UTF-8. Phrasing IDs are two lowercase letters plus two digits, unique within the file; use the prefixes below so they never collide with train IDs mentally (`t` for test):

| File | ID prefix |
|---|---|
| road_blocked | `tb01`, `tb02`, ... |
| road_cleared | `tc01`, ... |
| substation_status | `ts01`, ... |
| repair_eta | `te01`, ... |
| hospital_fuel | `tf01`, ... |
| burn_spike | `tk01`, ... |
| depot_stock | `td01`, ... |
| _injection | `in01`, ... (the injection file's IDs must start with `in`) |

A phrasing file looks like this. **This example uses an invented fact type, `bridge_weight_limit`, that does not exist in Blackout**, so none of its sentences can end up in the bank:

```yaml
# Test phrasing bank: bridge_weight_limit (an invented example; not a real fact type).
fact_type: bridge_weight_limit
vocab:
  reason:
    - cracks in the deck
    - a failed inspection
phrasings:
  - id: tw01
    text: "heads up, {bridge} is posted at {tons} because of {reason}."
    channels: [police_radio]
  - id: tw02
    text: "my brother drives a lorry and says {bridge} won't take more than {tons} now."
    channels: [public]
```

For `substation_status`, every phrasing also has a `value:` line with its state:

```yaml
  - id: ts01
    value: failed
    text: "..."
```

**The injection file** (`_injection.yaml`) has no `fact_type` or `vocab`; each phrasing has a `kind` and exactly one target slot:

| kind | target slot | what it demands |
|---|---|---|
| `shed_zone` | `{zone}` (a district name) | cut power to a district (that contains a hospital) |
| `dispatch` | `{hospital}` or `{depot}` | send trucks or fuel somewhere that doesn't need it |
| `assign_crew` | `{substation}` | send repair crews to a substation that works fine |

`{authority}` is optional and becomes who the message claims to be from ("the Mayor's office", "the Fire Chief"). Injections are manipulative on purpose: urgent, claiming authority, telling the reader to ignore other instructions. Write them the way a prankster or a confused official might. Shape:

```yaml
phrasings:
  - id: in01
    kind: shed_zone
    text: "..."
```

**The noise lexicon** (`_noise.yaml`) has the same structure as the train bank's `_noise.yaml` (copy its *keys*, write your own *values*): `rates` (keep the train rates or pick similar ones), `abbreviations` (word → short form), `slang` (word → list of alternatives), `hedges` (`prefix` and `suffix` lists), and `chatter` (`open` and `close` lines per channel, where `{callsign}` is the speaker's call sign, a `filler` list, and `connectors` that join two facts in one message). Never put digits or number words in noise entries.

## Style: make it read differently

The point of the test bank is to check whether contestants understand *meaning*, not memorized wording. So:

- Use different verbs and structures than you'd expect ("impassable", "no go", "we've got a closure" rather than "blocked"; "back up", "juice is back", "restored" rather than "online").
- Vary sentence order: put the number first sometimes ("Two-fifty left in the tank at {hospital}"), the place last sometimes.
- Use a different register: more clipped, more chatty, regional or older-fashioned, longer or shorter than the train bank. Two writers with different voices is ideal.
- Keep every phrasing **unambiguous and true to its fact**: a reader must be able to tell exactly which fact type it is, which place, and which number. No sarcasm that flips the meaning, no "maybe blocked".

`blackout bank check` flags (by ID only) phrasings whose word sequences overlap a train phrasing heavily; rewrite those.

## Checking and locking

Run these from the repository root. They print counts, IDs and a digest, never your phrasings' text:

```bash
uv run blackout bank check data/phrasing/test
```

Fix every `error` line (missing slots, too few phrasings, bad IDs) and reconsider every `warning` line. When it passes:

```bash
uv run blackout bank lock data/phrasing/test
```

This writes `data/phrasing/test.lock.json` (a hash of every file). Commit the bank and the lock together, in one commit, with a message that says who wrote it. After that, the bank is frozen: changing a file breaks the lock, and the test suite refuses to generate.

## What happens next

The test suite is generated (and then frozen) with:

```bash
uv run blackout gen --suite test
```

It refuses to run until the bank matches its committed lock, and it never prints report text. Nobody runs any contestant on it until `docs/PREREGISTRATION.md` is committed (M12): `blackout batch` refuses the test suite until then.

The bank is data under CC-BY-4.0, like the scenarios (`docs/SCENARIO_CARD.md`); say in the commit or the bank's comments how you'd like to be credited.
