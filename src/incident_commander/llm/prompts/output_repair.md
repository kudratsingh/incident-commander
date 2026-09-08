Your previous `record_output` call failed validation:

{error}

Produce the same content again, with every field in the exact JSON shape the `record_output` schema defines.

- Nested objects are objects and nested arrays are arrays. Do not JSON-encode a field's value into a string; `"next_action": {"kind": "remediate", ...}` is correct, `"next_action": "{\"kind\": \"remediate\", ...}"` is not.
- Emit no delimiter that is not part of the value — no trailing bracket or brace left over from an enclosing structure.
- Include every required field, and no field the schema does not define.

This is a formatting correction and nothing else. Do not revise your findings, change which resource you named, soften a decision, or add new claims: reproduce the same content in the shape the schema asks for. You get one correction — the run escalates to a human if this call does not validate.
