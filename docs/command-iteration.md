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
names. `form` is supplied by an enclosing multiple-form scope; `database` remains reserved. The former `match` and `iteration`
namespaces are rejected. Field targets provide `field.code`, `field.index`, and
`field.<name>` / `field.<number>` captures. Capture names `code` and `index` are
reserved to prevent overriding built-in members.

`command_templates.py` validates placeholder syntax and performs one substitution
pass over supported parameter values, including literal containers and FunctionCall
arguments. Dictionary keys remain literal. Field selectors resolve `vars` and enclosing `form`
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

## Form scopes

WithForm accepts exactly one of `form` or `formPattern`. The shared Target radio
uses Single / Multiple for both fields and forms. A form pattern uses fullmatch
against names in the active database, with local names overriding remote names.
Targets are frozen by ID in `(name.casefold(), ID)` order. `form.name`,
`form.index`, and numbered/named captures remain fixed; name/index are reserved
capture names. Pattern substitution permits only vars.

The connected runner evaluates each following authored command across targets
sequentially, stopping globally on error. A new WithForm replaces the scope.
Each target has an independent inherited variable mapping; names assigned by
SetVariable inside the scope are discarded from the outer mapping on exit.
An empty selection warns and skips applications until a new switch.

FormWorkspace.active_slot selects which opschema entry update() changes. A scope
appends one visit per target and reuses those slots for subsequent commands,
while ordinary switches keep ordered visit behavior. Command snapshots are deep
copies. CommandExecution.form_iterations identifies scope summaries. _form_summary
recursively flattens nested results into concrete `iterations`, carrying tuple
application_context paths (form / field) and preserving every leaf snapshot.
field_iterations marks empty field groups so zero matches contribute no leaf.
Parent messages/diagnostics still include zero-match warnings.

Field targets are planned for every scoped form before application, then passed
to _application_steps; early failure retains all remaining planned leaves as
not_executed. Nested evaluation does not replace the published flat parent with
a temporary field-only summary. All repeated commands expand upward and count
concrete leaves, including WithForm and scoped single commands/SetVariable.
Selection indexes the flat list and displays that leaf's complete opschema with
its affected form selected. Execution still consumes the ordered snapshots.
