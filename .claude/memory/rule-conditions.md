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
