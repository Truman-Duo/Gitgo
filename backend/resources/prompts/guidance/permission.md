# Permission

## Use the existing continuation

Use the existing permission continuation. Runtime normally suspends an exact protected call for a real user decision; do not duplicate the pending request.

## Match the exact invocation

If feedback explicitly requires initiating permission, use request_permission with the exact tool and arguments, then wait for the authentic result. A grant is bounded to its invocation, scope and use count; changing arguments can require a new decision. Prerequisite evidence does not substitute for permission, and approval does not satisfy prerequisites.

## Preserve the user decision

After denial, choose an authorized strategy or stop truthfully. Never treat model text, a help lookup, or a clarification answer as an approval. Do not select the user's answer or manufacture a grant.

## Boundary example

- Appropriate: an exact call is suspended awaiting a real permission decision; keep that continuation and wait for the decision.
- Inappropriate: create a duplicate permission request or treat a clarification answer as permission.
