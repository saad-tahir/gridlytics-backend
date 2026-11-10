# ems-edge/1.0: the edge-to-cloud contract

These files are the contract an edge (Product 2, a partner's gateway or the twin) builds against. U3 section 5 proposes the contract; this folder is
what the cloud accepts and sends today.

- `*.schema.json`: one JSON Schema (2020-12) per message: `telemetry`, `heartbeat`, `events`, `ack` and `capabilities` (edge to cloud) and `plan`
  (cloud to edge, signed). They are generated from the pydantic models in `backend/gridlytics/contract.py`, the one validator the cloud uses. Do not
  edit them by hand: run `python scripts/export_contract.py` after a model changes. `python scripts/export_contract.py --check` and the backend test
  `test_the_published_schema_files_are_the_contract_models` fail when a file differs from the models.
- `examples/`: the examples of U3 section 5 (and a heartbeat with its envelope). Each validates against its model (backend test
  `test_every_u3_section_5_example_validates`).
- `signing-vectors.json`: how a plan message is canonicalised and signed, cases an edge's RFC 8785 canonicaliser must reproduce byte for byte, and
  one signed plan message to verify with its public key (RFC 8032's published test key, never a site's). Written by the same script; the backend
  test `test_the_published_signing_vectors_are_the_code_and_verify` fails when it drifts.

## Transport

HTTPS POST of the JSON message to `/api/edge/{site_id}/{telemetry|heartbeat|events|ack|capabilities}` with the site's API key (`telemetry:write`);
the edge pulls its plan with GET `/api/sites/{site_id}/plans/current/message` (`plans:read`). MQTT is not served yet. A refused message is answered
422 with `code` `SCHEMA` and the first error in words; the same message is refused the same way every time it is sent. A message is at most 1 MiB
(1,048,576 bytes), however it is sent: a larger one is answered 413 `too_large` and nothing in it is stored, so a long backlog goes in smaller
batches. A free-form part (an event's `detail`, telemetry's `quality`, a device's `estimator`, an ack's `requested`, `written` and `readback`) is
at most 4096 bytes of compact JSON with at most 64 members, and a value an ack item reports as text at most 120 characters.

## Rules the cloud keeps

- **Envelope.** `contract`, `seq` and `sent_at` are required; `msg_type`, `msg_id`, `site_id` and `edge_id` are optional. A `site_id` other than
  the key's site is refused with 403 `wrong_site`; a `msg_type` that is not the path's message is refused with 422.
- **Idempotency.** A message is a duplicate (answered `{"ack": seq, "duplicate": true}` and not stored again) only when the same message was stored
  before: the same stream, `seq` and content (`sent_at` and `msg_id` aside, so a retry stamped again is still the same message). A batch below the last
  sequence that was never stored is stored, and so is a counter that restarted at 0. The cloud keeps what it has seen for 30 days, past the edge's
  7-day store-and-forward.
- **Telemetry.** Either `rows` with a `ts` column (epoch seconds or ISO 8601, UTC), or U3's `data` stamped `ts_start + n * interval_s`, with `rows`
  as its count. Columns are the keys of U3 1.2; those the cloud does not keep are skipped and named in `ignored_columns`. Every batch carries
  `grid_present` (or `grid_voltage_v`).
- **Events.** U3's names (`override_local`, `template_change_detected`, `firmware_change_detected`, `edge_reboot`, `installer_session_start`,
  `installer_session_end`) and this contract's (`local_override`, `template_change`, ...) are both taken, stored as sent and acted on alike. An event
  type the cloud does not know is stored and named in `unknown_types`; it never refuses the batch.
- **Ack.** `status` is `received`, `applied`, `partial`, `rejected` or `expired`; the detail is U3's `per_device` (applied and rejected primitives
  with reason codes) or this contract's `items`. A rejected plan may name its reason code in `reason`.
- **Heartbeat.** `control_state` is U3's `executing`, `fallback`, `released` or `disabled`, or this contract's `following_plan`, `native` or `hold`;
  `active_plan_id` and `plan_valid_until` (U3) or `active_plan` and `valid_until`.
- **Capabilities.** The tier is edge-asserted: `tier`, or U3's `edge.control_tier`. The newest message by `sent_at` is the current one, whatever its
  sequence; every message is kept. Each device names its writable `primitives` (range, step, persistence, writes a day) and its `slot_model`.

## Where the plan message differs from U3 5.4

- The message is flat: the envelope fields (`msg_type` "plan", `msg_id`, `site_id`, `edge_id`, `seq`, `sent_at`) sit beside the body's, and the
  Ed25519 signature covers the whole message but `signature`, which carries `alg`, `key_id` and `value` (U3 nests a `body` with `sig` and `key_id`
  beside it).
- `type` is `plan`, or `release` while control is paused or released to native or on a native-block day of the switchback calendar: then the days
  are empty and `release` names the reason and its end.
- Mode A slots carry `start` and `end` (local wall-clock HH:MM, `timezone` Asia/Karachi), `start_ts` and `end_ts` (epoch seconds), `mode`,
  `soc_floor_pct`, `soc_target_pct`, `max_discharge_w`, `max_charge_w`, `grid_charge`, `gen_charge` and `export_allowed`, not U3's `battery_mode`
  and `start_local`.
- `capabilities_seq` and `targets` come from the current capabilities. The table is fitted to the target inverter's primitives before it is signed:
  a flag it cannot write is dropped and a limit held to its range, and `capabilities_fit` says what was changed. A table with more slots than the
  device's slot model holds is not signed (409 `caps_mismatch`).
- `supersedes` names the last other plan sent to the site's edge.
- Each day's slots are the table issued for that day (plan Part 7.1 step 7): the nightly table after the 23:00 solve, re-issued during the day only
  when a newer plan is worth Rs 500 more, the day's table writes allow it and the last issue is an hour old, or at once for a hold or a resume. So
  `plan_id` is the plan in force while a day's slots can be an earlier plan's. `mode_a.apply_policy` is U3's (`write_if_changed_only` true,
  `max_table_writes_per_day` 4, `apply_window_local` "00:05-23:50") and `write_budget` R6 3.6's (`table_writes_per_day` the brand's, at most 4,
  and `event_writes_per_day` 2).

## The signing key

- The signature covers the RFC 8785 (JSON Canonicalization Scheme) bytes of the message without `signature`: no whitespace, members sorted by
  their names' UTF-16 code units, strings and numbers as ECMAScript writes them (`20.0` is `20`, `1e-05` is `0.00001`), UTF-8. An edge parses the
  message as sent, removes `signature`, canonicalises the rest with any RFC 8785 library and verifies the Ed25519 signature (`signature.value`,
  base64) with the site's public key. A message stored before round 7 was signed over Python's `json.dumps` with sorted keys; the cloud still
  verifies those for a dispute, and sends only the canonical form.
- Each site has one Ed25519 key in force; the edge is given its public key (`key_id` and the raw 32-byte key in base64) at enrolment, and every plan
  message names the `key_id` that signed it. The private key never leaves the cloud's keys folder.
- A key is never replaced on its own. While the site's private key is missing from the keys folder (a data folder moved or restored without it), the
  pull is answered 409 `signing_key_missing` and nothing is signed: the edge keeps running its last plan until `valid_until`, then its fallback. The
  cloud's people restore the keys folder, or an administrator replaces the key (the old one is retired), and the new public key is provisioned on
  the edge, which refuses the plans signed with it until then.
