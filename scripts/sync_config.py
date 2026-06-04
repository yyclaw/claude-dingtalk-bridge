#!/usr/bin/env python3
"""Sync an existing user config with the template.

`make config` runs this when the config already exists. It reconciles
config.example.yaml against the installed config at top-level-key granularity:

  * a key the template has but the user lacks (and didn't merely comment out)
    is appended at the end, whole block (leading comment + nested lines) verbatim;
  * an active key the user has but the template no longer carries is removed,
    whole block and its trailing blank separator;
  * a key the user deliberately commented out is left untouched -- "absent" and
    "present but commented" are different;
  * a key present in both has its leading comment lines refreshed from the
    template; the user's value lines stay byte-for-byte unchanged, and any
    user-disabled config key absorbed into the leading range is preserved.

Nested keys differing inside a block can't be added/removed at the end without
breaking structure, so those are only reported for manual review.

Run:  python scripts/sync_config.py <path-to-user-config>
The file is rewritten only when a top-level block is added, removed, or has
its leading comments refreshed; the bodies of kept blocks are never touched.
"""
import pathlib
import re
import sys

import yaml

EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "config.example.yaml"
TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*):")
COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Za-z_][\w-]*)\s*:")
NESTED_KEY = re.compile(r"^\s+([A-Za-z_][\w-]*)\s*:")

# One template section is reconciled at nested-key granularity rather than the
# whole-block granularity the rest of the script uses. REQUIRED leaves must
# merely exist (appended to an old user config that predates them); OVERWRITE
# leaves are forced to the template's exact value wherever they appear, since the
# daemon depends on the canonical value. Naming concrete fields here is a
# deliberate, user-approved exception to the otherwise-generic design.
GEO_KEY = "geo"
GEO_REQUIRED_LEAVES = ("country_field", "ip_field")
GEO_OVERWRITE_LEAVES = ("geo_service",)


def leaves(node, prefix=""):
    """Leaf-key paths; dicts recurse, lists/scalars are single leaves."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                yield from leaves(v, p)
            else:
                yield p


def top_level_blocks(lines):
    """Map each top-level key to its block of lines (leading comment + body)."""
    blocks = {}
    for key, s, e in top_level_spans(lines):
        block = lines[s:e]
        # Trailing blanks/comments belong to the next key's separator/heading.
        while block and (not block[-1].strip() or block[-1].lstrip().startswith("#")):
            block.pop()
        blocks[key] = block
    return blocks


def split_block(block):
    """Split a top-level block into (leading_comments, body_from_key_line)."""
    for i, ln in enumerate(block):
        if TOP_KEY.match(ln):
            return block[:i], block[i:]
    return block, []


def protected_lines(lines, known):
    """Indices inside a commented-out key block the user disabled on purpose --
    a run of comment lines whose `# key:` names a real config key (in `known`).
    Such a block must survive even when an adjacent block is removed (the
    removal span's leading-comment walk-back would otherwise sweep it in).

    Prose comments that merely happen to read like `# Word:` (e.g. `# Footguns:`
    in a documentation block) are NOT config keys, so they stay unprotected and
    are removed together with the block they document."""
    protected, i, n = set(), 0, len(lines)
    while i < n:
        if not lines[i].lstrip().startswith("#"):
            i += 1
            continue
        j = i
        while j < n and lines[j].lstrip().startswith("#"):
            j += 1
        if any((m := COMMENTED_KEY.match(lines[k])) and m.group(1) in known
               for k in range(i, j)):
            protected.update(range(i, j))
        i = j
    return protected


def top_level_spans(lines):
    """Ordered (key, start, end) spans partitioning everything below the header.

    start reaches back over the key's leading comment lines; spans are
    contiguous (a span ends where the next key's starts, the last at EOF), so
    deleting one removes the block and its trailing blank separator cleanly.
    """
    idx = [i for i, ln in enumerate(lines) if TOP_KEY.match(ln)]
    starts = []
    for i in idx:
        s = i
        while s - 1 >= 0 and lines[s - 1].lstrip().startswith("#"):
            s -= 1
        starts.append(s)
    spans = []
    for n, i in enumerate(idx):
        key = TOP_KEY.match(lines[i]).group(1)
        end = starts[n + 1] if n + 1 < len(idx) else len(lines)
        spans.append((key, starts[n], end))
    return spans


def _comment_body(line):
    """Strip one leading `# ` from a commented line to expose the YAML it hides
    (so indentation under a commented-out block can be read); None if the line
    isn't a comment."""
    m = re.match(r"^(\s*)#[ ]?(.*)$", line)
    return None if m is None else m.group(1) + m.group(2)


def required_leaf_template(lines, key, leaves):
    """Verbatim template line for each of `leaves` nested under top-level `key`."""
    out, inside = {}, False
    for ln in lines:
        if re.match(rf"^{re.escape(key)}\s*:", ln):
            inside = True
            continue
        if inside:
            if ln.strip() and not ln[:1].isspace():  # next top-level key
                break
            if (m := NESTED_KEY.match(ln)) and m.group(1) in leaves:
                out[m.group(1)] = ln
    return out


def _find_block_head(lines, key):
    """(index, commented?) of the line opening top-level `key`, active or
    commented-out; (None, None) if absent."""
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", ln):
            return i, False
        if re.match(rf"^\s*#\s*{re.escape(key)}\s*:", ln):
            return i, True
    return None, None


def sync_block_leaves(lines, key, required, overwrite, leaf_template):
    """Reconcile the user's `key` block against the template at nested-key
    granularity (unlike the whole-block reconciliation elsewhere). `required`
    leaves are appended when absent; `overwrite` leaves are forced to the
    template's exact line wherever they appear, and appended when absent. Works
    whether the block is active or commented out, keeping inserted/replaced lines
    in the same comment state. Returns (new_lines, added, overwritten)."""
    head, commented = _find_block_head(lines, key)
    if head is None:
        return lines, [], []

    def styled(leaf):
        return f"# {leaf_template[leaf]}" if commented else leaf_template[leaf]

    lines = list(lines)
    present, overwritten, last_nested, j = set(), [], head, head + 1
    while j < len(lines):
        body = _comment_body(lines[j]) if commented else lines[j]
        if commented:
            if body is None:                                   # left the block
                break
            if body.strip() and not body[:1].isspace():        # next top-level
                break
        elif lines[j].strip() == "" or not lines[j][:1].isspace():
            break
        if m := NESTED_KEY.match(body):
            name = m.group(1)
            present.add(name)
            if name in overwrite and name in leaf_template and lines[j] != styled(name):
                lines[j] = styled(name)
                overwritten.append(name)
            last_nested = j
        j += 1

    added = [k for k in (*required, *overwrite)
             if k in leaf_template and k not in present]
    insert = [styled(k) for k in added]
    new_lines = lines[:last_nested + 1] + insert + lines[last_nested + 1:]
    return new_lines, added, overwritten


def main():
    user_path = pathlib.Path(sys.argv[1])
    example_text = EXAMPLE.read_text()
    user_text = user_path.read_text()
    example = yaml.safe_load(example_text) or {}
    user = yaml.safe_load(user_text) or {}

    example_lines = example_text.splitlines()
    user_lines = user_text.splitlines()

    example_blocks = top_level_blocks(example_lines)
    active_top = list(user) if isinstance(user, dict) else []
    commented = {m.group(1) for ln in user_lines
                 if (m := COMMENTED_KEY.match(ln))}

    to_add, skipped = [], []
    for key in example:
        if key in active_top:
            continue
        (skipped if key in commented else to_add).append(key)
    # A key the template carries only commented-out (an opt-in example) is still
    # "known" -- don't remove the user's active value just because of that.
    template_known = set(example) | {m.group(1) for ln in example_lines
                                     if (m := COMMENTED_KEY.match(ln))}
    to_remove = [k for k in active_top if k not in template_known]

    user_leaves, example_leaves = set(leaves(user)), set(leaves(example))
    nested_missing = [p for p in leaves(example)
                      if p not in user_leaves and p.split(".")[0] in active_top]
    nested_extra = [p for p in leaves(user)
                    if p not in example_leaves and p.split(".")[0] in example]

    spans = top_level_spans(user_lines)
    # yaml.safe_load accepts keys whose lines our regex-based span finder can't
    # locate (unusual leading characters, quoted forms, etc.). We can't safely
    # modify what we can't locate, so split such keys off and surface them.
    findable = {k for k, _, _ in spans}
    unreachable = [k for k in to_remove if k not in findable]
    to_remove = [k for k in to_remove if k in findable]

    protected = protected_lines(user_lines, template_known)
    template_leading = {k: split_block(b)[0] for k, b in example_blocks.items()}

    # Detect kept keys whose leading comments diverged from the template. The
    # comparison ignores protected lines (user-disabled keys absorbed into the
    # leading range via comment walk-back) so we don't reorder them on a no-op.
    comment_updates = []
    remove_set = set(to_remove)
    for key, s, e in spans:
        if key not in example_blocks or key in remove_set:
            continue
        user_leading = []
        for i in range(s, e):
            if TOP_KEY.match(user_lines[i]):
                break
            if i not in protected:
                user_leading.append(user_lines[i])
        if user_leading != template_leading[key]:
            comment_updates.append(key)

    top_level_changed = bool(to_add or to_remove or comment_updates)
    if top_level_changed:
        updates_set = set(comment_updates)
        new_lines = []
        prev_end = 0
        for key, s, e in spans:
            new_lines.extend(user_lines[prev_end:s])
            if key in remove_set:
                for i in range(s, e):
                    if i in protected:
                        new_lines.append(user_lines[i])
                prev_end = e
                continue
            key_line = s
            while key_line < e and not TOP_KEY.match(user_lines[key_line]):
                key_line += 1
            if key in updates_set:
                for i in range(s, key_line):
                    if i in protected:
                        new_lines.append(user_lines[i])
                new_lines.extend(template_leading[key])
            else:
                new_lines.extend(user_lines[s:key_line])
            new_lines.extend(user_lines[key_line:e])
            prev_end = e
        new_lines.extend(user_lines[prev_end:])

        text = "\n".join(new_lines).rstrip("\n")
        for k in to_add:
            text += "\n\n" + "\n".join(example_blocks[k])
        result_lines = text.split("\n")
    else:
        result_lines = list(user_lines)

    geo_leaves = (*GEO_REQUIRED_LEAVES, *GEO_OVERWRITE_LEAVES)
    geo_template = required_leaf_template(example_lines, GEO_KEY, geo_leaves)
    result_lines, geo_added, geo_overwritten = sync_block_leaves(
        result_lines, GEO_KEY, GEO_REQUIRED_LEAVES, GEO_OVERWRITE_LEAVES, geo_template)
    nested_missing = [p for p in nested_missing
                      if p not in {f"{GEO_KEY}.{k}" for k in geo_added}]

    if top_level_changed or geo_added or geo_overwritten:
        user_path.write_text("\n".join(result_lines).rstrip("\n") + "\n")

    if to_add:
        print("Added new keys from the template (appended at the end):")
        for k in to_add:
            print(f"  + {k}")
    if to_remove:
        print("Removed keys no longer in the template:")
        for k in to_remove:
            print(f"  - {k}")
    if unreachable:
        print("Keys not in the template but can't be auto-removed (edit manually):")
        for k in unreachable:
            print(f"  ? {k}")
    if comment_updates:
        print("Refreshed leading comments from the template:")
        for k in comment_updates:
            print(f"  * {k}")
    if geo_added:
        print(f"Added required {GEO_KEY} subkeys the template carries:")
        for k in geo_added:
            print(f"  + {GEO_KEY}.{k}")
    if geo_overwritten:
        print(f"Overwrote {GEO_KEY} subkeys with the template value:")
        for k in geo_overwritten:
            print(f"  = {GEO_KEY}.{k}")
    if skipped:
        print("Skipped keys you commented out (left as-is):")
        for k in skipped:
            print(f"  ~ {k}")
    if nested_missing:
        print("New nested keys in the template (add manually if you want them):")
        for p in nested_missing:
            print(f"  - {p}")
    if nested_extra:
        print("Nested keys not in the template (remove manually if unwanted):")
        for p in nested_extra:
            print(f"  + {p}")


if __name__ == "__main__":
    main()
