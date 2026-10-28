---
name: rule-conditions
description: "Read this file before you change the rule format, the rule evaluator or the rule editor."
metadata:
  type: reference
---

- A rule keeps its conditions as a JsonLogic tree of AND, OR and NOT groups. JsonLogic has "in", comparisons and string operators, but no regular expression.
- `datalogic-py` evaluates JsonLogic and accepts a custom operator, for example `regex`. Its import name is `datalogic_py`. A custom operator gets its arguments as a JSON string and returns a JSON string.
- `Engine.truthy` exists only from `datalogic-py` 5.8.0, and 5.7.1 does not have it. The Python `bool` of the parsed result agrees with the `!!` of the engine for each JSON value, also `{}` and `"0"`.
- A rule reads a channel only by its `UC...` id, never by the channel name or the handle. A channel can change both, and another channel can take them later.
- NiceGUI has no condition builder. TinyVue QueryBuilder builds the tree in Vue 3 and exports JsonLogic, but it needs a JavaScript build.
- `{"and": []}` and `{"or": []}` match no video, and `MATCH_ALL`, `{"==": [1, 1]}`, matches each video. The editor saves a condition without leaves as `MATCH_ALL`. It opens an empty `and` or `or` as JSON text, because its empty group means each video.
- The visual editor opens a tree only when the tree comes back unchanged from the editor model: `from_tree` compares `to_tree` of the model with the tree. Each other tree opens as JSON text, so a save without a change never changes a stored condition.
- A flat entry has a `live_status` only for a live stream, for example `is_upcoming` or `was_live` in the recorded answers. A normal video has `live_status` null, and the evaluator counts a field without a value as unknown. So `Video.fields` gives `not_live` for an entry without a live status, as `is_finished` already counts it finished. Without that, "`live_status` is not `was_live`" gave UNKNOWN for each normal video, and the video stayed deferred.
- The editor and its preview never show the words "baseline", "source", "seen" or "processing mode", because a user does not know the inside of the service. The editor tests check this with `inner_words`.
- A change of a field of the editor does not remove the shown preview. The user knows that it is the result of the last press of Preview, and the next press replaces it. A preview that ends after a change shows its result too. The apply dialog differs: it forgets a preview of another day on purpose.
- A saved rule opens with its stored catch-up day, which is empty after the next run did the catch-up. A save of an empty field would otherwise remove a day that still waits for a run.
- `validate` refuses a tree deeper than `MAX_DEPTH`, 32 levels, where each object and each list counts. The JSON output of pydantic fails at about 97 levels with "Circular reference detected", and a tree of 3000 levels reached the recursion limit of Python. It also refuses NaN and Infinity, which `json.loads` reads but a run cannot evaluate.
