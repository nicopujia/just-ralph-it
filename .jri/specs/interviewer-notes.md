# Interviewer Notes

## Model

The project is a graph with two primitives:

- **Note:** a node with a stable ID and text.
- **Connection:** a directed, labeled relationship from one note to another.

Topics, subtopics, and smaller ideas are all notes. Connections express both hierarchy and cross-topic relationships; neither is stored as a special note field. Backlinks are derived from connections.

A note holds one independently meaningful topic or statement. Larger ideas are represented as multiple connected notes rather than separate title and content fields.

The reserved `root` note is the graph entry point. Connection semantics, topic-tree invariants, and focus behavior are defined by [Interviewer Context Management](interviewer-context-management.md).

## Interviewer Tools

The interviewer has these foundational note operations:

- `read_notes`: fuzzy search for notes, read notes by ID, and traverse their connections.
- `add_notes`: create and classify one or more notes under a project or topic.
- `edit_note`: edit one existing note without changing its connections.
- `delete_notes`: delete one or more notes and every connection touching them.
- `connect_notes`: create one or more labeled connections.
- `disconnect_notes`: remove one or more connections.

Context management adds atomic reclassification and focus switching. All
connections use the canonical labels defined in that specification.

The user interacts only through normal conversation. The interviewer manages notes and connections automatically.

## Bulk Operations

`read_notes`, `add_notes`, `delete_notes`, `connect_notes`, and `disconnect_notes` permit bulk input. `edit_note` and reclassification remain singular so each revision has one explicit target.

Bulk mutations are atomic: either every requested change is valid and applied, or none are. Adding notes creates their classification connections in the same mutation so successful operations never leave orphans. Connecting an existing connection and disconnecting a missing connection are idempotent no-ops.

## Persistence

All notes and its connections are saved at `.jri/graph.json` like this:

```json
{
  "notes": [...],
  "connections": [...]
}
```
