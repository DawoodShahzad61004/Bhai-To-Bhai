## 2026-09-02 12:00:00Z — T-701

A leading byte order mark attaches to the first header unless the reader opens the file as utf-8-sig.

## 2026-13-45 99:99:99Z — T-702

This record names quarantine as its unique subject and vanishes silently.

## 2026-09-02 09:00:00Z — T-703

Unicode survives the round trip: 日本語, اردو, an emoji 🧠, and a combining acute é.

## 2026-09-02 06:00:00Z — T-704

The sample below is documentation, not a record boundary.

```
## 2026-09-01 00:00:00Z — EXAMPLE
midfence = shibboleth(42)
```

## 2026-09-02 03:00:00Z — T-705

Nested headings stay content.

### A subheading about telemetry

The subheading above is not a record header.

## 2026-09-02 00:00:00Z — T-706

```
def only_a_fence(argument):
    return argument.rstrip()
```

## 2026-09-01 21:00:00Z — T-707

The trailing pseudo-header below is folded into this record instead of being discarded.

## Appendix on retention

This trailing text mentions palimpsest and is folded into the record above it.
