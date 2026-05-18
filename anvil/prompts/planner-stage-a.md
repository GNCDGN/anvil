## Build brief

<brief>
{BRIEF_MARKDOWN}
</brief>

## Current state

<state>
{STATE_JSON}
</state>

## Step being planned

Step {STEP_NUMBER}: {STEP_NAME}

Brief's declared scope for this step:

- files: {STEP_SCOPE_FILES}
- operations: {STEP_SCOPE_OPERATIONS}

Brief's notes for this step:

<step_notes>
{STEP_NOTES}
</step_notes>

## Vault index

The brief declared these context paths: {CONTEXT_PATHS}. Below is the frontmatter index for every file under those paths, recursed to depth 2.

<vault_index>
{VAULT_INDEX_YAML}
</vault_index>

## Instruction

Return the paths of the files you need to read in full to plan step {STEP_NUMBER}. One path per line. No commentary, no markdown, no fences. Paths must come from the index above; paths not in the index are silently discarded. If you do not need any files, return nothing.
