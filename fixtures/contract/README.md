# Contract fixtures

Sanitized payloads shaped like the real source responses, used by the adapter
tests. Identifiers, project names and tool IDs are synthetic; the structures,
field nesting and event sequences follow observations captured from a CI
instance and the AnVIL dev instance.

| File | Shape and scenario |
| --- | --- |
| `batch-job-retry.json` | A Batch job whose task events contain a provider retry on the same VM, including the `RETRIED` event and exit code 125 |
| `batch-job-galaxy-error.json` | A Batch resource reporting `SUCCEEDED` for a job Galaxy recorded as an error |
| `compute-instance.json` | A Compute instance description for that VM, with its provider-created Batch label |
| `logging-entries.json` | Insert and delete operation markers that recover the VM lifecycle window after deletion |
| `kubernetes-pods.json` | Job pods on the baseline node and on an unmatched node, including a container restart and running work |
| `kubernetes-nodes.json` | Node descriptors with and without a provider ID |

No credentials, endpoints, command lines, logs or dataset contents appear here.
Live operational evidence stays outside this repository.
