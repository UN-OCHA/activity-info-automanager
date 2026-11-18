# Command iteration internals

`command_iteration.py` owns authored `IterativeCommand` values, selector parsing,
and immutable target records. The existing catalog remains authoritative; no
plural command family is registered. Text parsing delegates ordinary parameter
validation to the existing command parsers and translates `fieldPattern` into a
shared selector wrapper. Creator target mode builds the same wrapper.

Patterns use `re.fullmatch` semantics, including Python inline flags. Empty and
invalid patterns are rejected during creation/parsing. Matches are discovered
once per authored command, from its incoming schema's top-level elements, in
schema order. Each target stores its original code, ID, and local variables.
Target IDs bind subsequent execution to the original field; ID-less schemas fall
back to an unambiguous code. Ambiguous or missing bindings fail explicitly.

`command_templates.py` centrally defines namespace member syntax: `field`,
`form`, and `database` accept named/numbered members; `vars` accepts variable
names. `form` and `database` are reserved and fail at execution because their
iteration context is not implemented. The former `match` and `iteration`
namespaces are rejected. Field targets provide `field.code`, `field.index`, and
`field.<name>` / `field.<number>` captures. Capture names `code` and `index` are
reserved to prevent overriding built-in members.

`command_templates.py` validates placeholder syntax and performs one substitution
pass over supported parameter values, including literal containers and FunctionCall
arguments. Dictionary keys remain literal. Field selectors resolve only `vars`
references before target selection; `field` context is rejected. This is text
substitution, not Python evaluation, recursive expansion, or formula escaping.
SetVariable writes `vars.name` text into a mapping owned by one command-list
evaluation. Iteration preparation merges that mapping with its local captures.
Variable names remain literal. Re-evaluation starts fresh. Pattern templates
are compiled after substitution, without automatic regex escaping. Iteration
preparation preserves the resolved target code without substituting it again.

Concrete commands are reconstructed after substitution to revalidate parameters.
`PreparedCommand` prevents the ordinary execution boundary from applying a second
substitution pass. Functions and schema resolution then follow the existing
pipeline. Non-iterative commands use the same substitution boundary with the
current evaluation variables, allowing literal escapes and rejecting undefined variables.

`CommandListManager._application_steps` is a generator shared by local and async
evaluation. It yields concrete applications and receives normal ExecutionResults.
MoveField uses the authored destination for the first target and `After field`
with the preceding target's code for every later application. Captures continue
to describe the original match.

The public `executions` list stays aligned with authored commands. Its entries
are summaries with `iterations` holding individual CommandExecution snapshots,
target metadata, and concrete commands. This preserves existing final-target and
Execute gating consumers. All warnings/errors are attributed in parent messages;
parent schema annotations come only from the final reached snapshot, avoiding
stale array paths from earlier iterations. Children retain their own diagnostics.

Selection has a parent index and optional child index. Expansion is presentation
state keyed by authored-command identity. Children cannot be independently
reordered/deleted; collapsing a selected child selects the parent. Source/list
changes cancel async evaluation and invalidate stale results. Display-name refresh
updates both parent and child caches without rerunning commands or substitution.
