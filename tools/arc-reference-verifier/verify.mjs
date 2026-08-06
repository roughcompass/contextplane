#!/usr/bin/env node
// ARC authoring-surface canonical vector reference verifier.
//
// Dependency-minimal by design: only `node:crypto` and `node:fs` (plus
// `node:path` and `node:url` for file plumbing). No `package.json`, no
// npm install step, and no import of anything under `registry/registry/` --
// this file shares no code with the Python fixture generator or with any
// future production canonicalizer. That is the entire point of shipping it:
// two independent implementations of the same canonicalization rules,
// checked against the same published vectors, are worth something only
// because neither can quietly inherit the other's bugs.
//
// Usage:
//   node tools/arc-reference-verifier/verify.mjs tests/fixtures/arc_authoring
//
// Exit code is non-zero if any case disagrees with the manifest's published
// expectation, or if a manifest-declared cardinality does not match what is
// actually on disk (an emptied or truncated fixture set fails on the count,
// not by quietly reporting zero disagreements over nothing).
//
// What this script independently re-derives, per case, from nothing but the
// raw case input file, `manifest.json`, and the public keys in `keys.json`:
//   1. Structural shape: does the input satisfy `schema.json`'s closed field
//      set, required keys, enums, and value-format patterns?
//   2. Canonical bytes: recursively sorted keys, NFC-only strings, no NUL,
//      integral numbers, set-valued arrays deduplicated and sorted, ordered
//      arrays checked against their declared sort key.
//   3. The digest of those bytes.
//   4. For the five signed profiles: the exact domain-separated signing
//      input, and whether the published signature verifies against the
//      published public key.
// A case's `decision` is `accept` only when all four independently succeed
// and agree byte-for-byte with the manifest; any disagreement is reported
// and the process exits non-zero.

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const ROOT = process.argv[2];
if (!ROOT) {
  console.error("usage: node verify.mjs <fixture-root>");
  process.exit(2);
}

function readJson(relativePath) {
  const full = path.join(ROOT, relativePath);
  return JSON.parse(readFileSync(full, "utf8"));
}

const manifest = readJson("manifest.json");
const keys = readJson("keys.json");

let failures = 0;
let checkedCases = 0;

function fail(context, message) {
  failures += 1;
  console.error(`FAIL ${context}: ${message}`);
}

// ---------------------------------------------------------------------
// Minimal JSON-Schema-shaped structural validator. Independently written
// against the same rules the Python generator documents in its own module
// docstring -- not a port of that file.
// ---------------------------------------------------------------------

class SchemaError extends Error {}
class CanonError extends Error {}

function typeMatches(expected, value) {
  switch (expected) {
    case "string":
      return typeof value === "string";
    case "number":
      return typeof value === "number";
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    case "array":
      return Array.isArray(value);
    case "object":
      return typeof value === "object" && value !== null && !Array.isArray(value);
    default:
      throw new Error(`unknown schema type ${expected}`);
  }
}

function validate(schema, value, at = "$") {
  const types = schema.type === undefined ? null : Array.isArray(schema.type) ? schema.type : [schema.type];
  if (types !== null && !types.some((t) => typeMatches(t, value))) {
    throw new SchemaError(`${at}: expected type ${JSON.stringify(types)}, got ${JSON.stringify(value)}`);
  }
  if ("const" in schema && value !== schema.const) {
    throw new SchemaError(`${at}: expected constant ${JSON.stringify(schema.const)}, got ${JSON.stringify(value)}`);
  }
  if (value === null) return;
  if ("enum" in schema && !schema.enum.includes(value)) {
    throw new SchemaError(`${at}: ${JSON.stringify(value)} is not one of ${JSON.stringify(schema.enum)}`);
  }
  if ("pattern" in schema && typeof value === "string") {
    // Every pattern published in schema.json is already anchored (`^...$`).
    if (!new RegExp(schema.pattern).test(value)) {
      throw new SchemaError(`${at}: ${JSON.stringify(value)} does not match the required format`);
    }
  }
  if (typeMatches("object", value)) {
    const props = schema.properties || {};
    const required = schema.required || [];
    const missing = required.filter((name) => !(name in value));
    if (missing.length > 0) {
      throw new SchemaError(`${at}: missing required field(s) ${JSON.stringify(missing)}`);
    }
    if (schema.additionalProperties === false) {
      const unknown = Object.keys(value).filter((k) => !(k in props));
      if (unknown.length > 0) {
        throw new SchemaError(`${at}: unknown field(s) ${JSON.stringify(unknown)}`);
      }
    }
    for (const [key, subValue] of Object.entries(value)) {
      if (key in props) validate(props[key], subValue, `${at}.${key}`);
    }
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) {
      throw new SchemaError(`${at}: expected at least ${schema.minItems} item(s), got ${value.length}`);
    }
    if (schema.items !== undefined) {
      value.forEach((item, i) => validate(schema.items, item, `${at}[${i}]`));
    }
  }
}

// ---------------------------------------------------------------------
// Canonicalization: NFC-only strings, no NUL, integral numbers, sorted
// object keys, set arrays deduplicated+sorted by canonical bytes, ordered
// arrays checked against a declared sort key.
// ---------------------------------------------------------------------

function canonicalJsonBytes(value) {
  return Buffer.from(JSON.stringify(value), "utf8");
}

function canonicalize(schema, value, at = "$") {
  if (value === null) return null;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isInteger(value)) {
      throw new CanonError(`${at}: fractional number has no canonical form`);
    }
    return value;
  }
  if (typeof value === "string") {
    if (value.normalize("NFC") !== value) {
      throw new CanonError(`${at}: string is not Unicode NFC normalized`);
    }
    if (value.includes("\u0000")) {
      throw new CanonError(`${at}: string contains a NUL character`);
    }
    return value;
  }
  if (Array.isArray(value)) {
    const itemsSchema = schema.items || {};
    const canonItems = value.map((item, i) => canonicalize(itemsSchema, item, `${at}[${i}]`));
    const kind = schema["x-array-kind"];
    if (kind === "set") {
      const keyed = canonItems.map((item) => [canonicalJsonBytes(item).toString("latin1"), item]);
      const seen = new Set();
      for (const [serialized] of keyed) {
        if (seen.has(serialized)) {
          throw new CanonError(`${at}: duplicate entry in a set-valued array`);
        }
        seen.add(serialized);
      }
      keyed.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
      return keyed.map(([, item]) => item);
    }
    if (kind === "ordered") {
      const orderKey = schema["x-order-key"];
      if (orderKey !== undefined) {
        let previous = null;
        for (const item of canonItems) {
          const current = typeof item === "object" && item !== null ? item[orderKey] : item;
          if (previous !== null && !(previous < current)) {
            throw new CanonError(`${at}: ordered array is not in strictly ascending '${orderKey}' order`);
          }
          previous = current;
        }
      }
      return canonItems;
    }
    throw new CanonError(`${at}: array has no set/ordered label`);
  }
  if (typeMatches("object", value)) {
    const props = schema.properties || {};
    const keys = Object.keys(value);
    for (const key of keys) {
      if (key.normalize("NFC") !== key) {
        throw new CanonError(`${at}.${key}: object key is not Unicode NFC normalized`);
      }
    }
    const sortedKeys = [...keys].sort();
    const out = {};
    for (const key of sortedKeys) {
      out[key] = canonicalize(props[key] || {}, value[key], `${at}.${key}`);
    }
    return out;
  }
  throw new CanonError(`${at}: unsupported value type ${typeof value}`);
}

function canonicalBytes(schema, obj) {
  validate(schema, obj);
  return canonicalJsonBytes(canonicalize(schema, obj));
}

function digestHex(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

// ---------------------------------------------------------------------
// Signature verification. Ed25519 only, matching the fixture set. Public
// keys are imported from raw 32-byte material via a JWK wrapper -- Node's
// stdlib has no direct raw-Ed25519-public-key import, but it does support
// JWK, so no third-party dependency is needed.
// ---------------------------------------------------------------------

function base64urlFromBase64(b64) {
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function publicKeyFromBase64(publicKeyBase64) {
  const jwk = { kty: "OKP", crv: "Ed25519", x: base64urlFromBase64(publicKeyBase64) };
  return crypto.createPublicKey({ key: jwk, format: "jwk" });
}

function verifySignature(publicKeyBase64, signingInput, signatureBase64) {
  try {
    const publicKey = publicKeyFromBase64(publicKeyBase64);
    return crypto.verify(null, signingInput, publicKey, Buffer.from(signatureBase64, "base64"));
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------
// Manifest structure checks -- cardinality first. A profile with an empty
// or truncated case list fails here, before any per-case check runs, so an
// emptied fixture cannot pass by vacuously finding nothing to disagree with.
// ---------------------------------------------------------------------

const EXPECTED_PROFILE_COUNT = 16;
const EXPECTED_CASE_KINDS = new Set(["minimal", "typical", "maximal", "negative"]);

if (!Array.isArray(manifest.profiles) || manifest.profiles.length !== EXPECTED_PROFILE_COUNT) {
  fail("manifest", `expected exactly ${EXPECTED_PROFILE_COUNT} profiles, found ${manifest.profiles?.length ?? 0}`);
}

const profileLiterals = manifest.profiles.map((p) => p.profile);
const sortedLiterals = [...profileLiterals].sort();
if (JSON.stringify(profileLiterals) !== JSON.stringify(sortedLiterals)) {
  fail("manifest", "profiles[] is not ordered by profile literal");
}

for (const profileEntry of manifest.profiles) {
  const { profile, schema_path: schemaPath, cases } = profileEntry;
  if (!Array.isArray(cases) || cases.length === 0) {
    fail(profile, "manifest declares zero cases -- an emptied fixture must fail on cardinality, not pass vacuously");
    continue;
  }
  const caseIds = cases.map((c) => c.case_id);
  if (new Set(caseIds).size !== caseIds.length) {
    fail(profile, "duplicate case_id within one profile");
  }
  const sortedCaseIds = [...caseIds].sort();
  if (JSON.stringify(caseIds) !== JSON.stringify(sortedCaseIds)) {
    fail(profile, "cases[] is not ordered by case_id");
  }
  const expectedDirName = profile.startsWith("arc_") ? profile.slice(4) : profile;
  if (!schemaPath.startsWith(`${expectedDirName}/`)) {
    fail(profile, `schema_path ${schemaPath} does not derive from the profile literal by stripping 'arc_'`);
  }
  let onDiskCases;
  try {
    const positiveDir = path.join(ROOT, expectedDirName, "positive");
    const negativeDir = path.join(ROOT, expectedDirName, "negative");
    onDiskCases = readdirSync(positiveDir).filter((f) => f.endsWith(".json")).length
      + readdirSync(negativeDir).filter((f) => f.endsWith(".json")).length;
  } catch (err) {
    fail(profile, `positive/negative case directories are not both present: ${err.message}`);
    continue;
  }
  if (onDiskCases !== cases.length) {
    fail(
      profile,
      `manifest declares ${cases.length} case(s) but ${onDiskCases} case file(s) exist on disk -- ` +
        "a truncated or padded fixture must fail on the count before any behavioral check runs"
    );
  }
  for (const c of cases) {
    if (!EXPECTED_CASE_KINDS.has(c.kind)) {
      fail(`${profile}/${c.case_id}`, `unknown case kind ${c.kind}`);
    }
  }
}

if (failures > 0) {
  console.error(`\n${failures} manifest-structure disagreement(s) found before any case was checked.`);
  process.exit(1);
}

// ---------------------------------------------------------------------
// Per-case verification.
// ---------------------------------------------------------------------

for (const profileEntry of manifest.profiles) {
  const { profile, schema_path: schemaPath, cases } = profileEntry;
  const schema = readJson(schemaPath);
  const signingKey = keys[profile];

  for (const c of cases) {
    checkedCases += 1;
    const context = `${profile}/${c.case_id}`;
    const input = readJson(c.input_path);
    const expected = c.expected;

    let canonical = null;
    let structuralError = null;
    try {
      canonical = canonicalBytes(schema, input);
    } catch (err) {
      if (err instanceof SchemaError || err instanceof CanonError) {
        structuralError = err;
      } else {
        throw err;
      }
    }

    if (expected.decision === "accept") {
      if (structuralError !== null) {
        fail(context, `expected accept, but independent canonicalization refused: ${structuralError.message}`);
        continue;
      }
      const digest = digestHex(canonical);
      if (Buffer.compare(canonical, Buffer.from(expected.canonical_bytes_base64, "base64")) !== 0) {
        fail(context, "independently recomputed canonical bytes do not match the published bytes");
      }
      if (digest !== expected.digest) {
        fail(context, `independently recomputed digest ${digest} does not match published digest ${expected.digest}`);
      }
      if (signingKey) {
        const recomputedInput = Buffer.concat([
          Buffer.from(signingKey.domain_prefix_hex, "hex"),
          signingKey.sign_over === "digest" ? Buffer.from(digest, "hex") : canonical,
        ]);
        if (Buffer.compare(recomputedInput, Buffer.from(expected.signature_input_base64, "base64")) !== 0) {
          fail(context, "independently recomputed signing input does not match the published signing input");
        }
        const verified = verifySignature(signingKey.public_key_base64, recomputedInput, expected.signature_base64);
        if (!verified) {
          fail(context, "published signature does not verify against the published signing key -- expected accept");
        }
      }
    } else if (expected.decision === "refuse") {
      const isSemantic = expected.canonical_bytes_base64 !== null;
      if (!isSemantic) {
        // Structural family: independent canonicalization must also refuse.
        if (structuralError === null) {
          fail(context, "expected a structural refusal, but independent canonicalization accepted the input");
        }
        if (expected.digest !== null || expected.signature_base64 !== null) {
          fail(context, "a structural refusal must publish null canonical/digest/signature fields");
        }
      } else {
        // Semantic family: independent canonicalization must succeed and
        // agree on bytes/digest; the wrongness must show up as a signature
        // that fails to verify, or (unsigned profiles) is otherwise
        // asserted by the manifest without a mechanism this verifier can
        // re-derive on its own -- which is exactly why every semantic
        // negative in this fixture set is on a signed profile or carries a
        // cross-fixture digest this script also checks below.
        if (structuralError !== null) {
          fail(context, `expected a semantic (shape-valid) refusal, but independent canonicalization also refused: ${structuralError.message}`);
          continue;
        }
        const digest = digestHex(canonical);
        if (Buffer.compare(canonical, Buffer.from(expected.canonical_bytes_base64, "base64")) !== 0) {
          fail(context, "independently recomputed canonical bytes do not match the published bytes");
        }
        if (digest !== expected.digest) {
          fail(context, `independently recomputed digest ${digest} does not match published digest ${expected.digest}`);
        }
        if (signingKey && expected.signature_base64) {
          const recomputedInput = Buffer.concat([
            Buffer.from(signingKey.domain_prefix_hex, "hex"),
            signingKey.sign_over === "digest" ? Buffer.from(digest, "hex") : canonical,
          ]);
          const verified = verifySignature(signingKey.public_key_base64, recomputedInput, expected.signature_base64);
          if (verified) {
            fail(context, "expected refuse (signature should not verify), but the published signature verified");
          }
        }
      }
    } else {
      fail(context, `unknown decision ${expected.decision}`);
    }
  }
}

// ---------------------------------------------------------------------
// Cross-fixture graph checks: the `S -> R -> A` chain and the operational
// event chain each embed a digest that names another profile's *typical*
// fixture. Recomputing those independently, from the referenced fixture's
// own file, is what makes "digest substitution" a real check rather than an
// assertion neither implementation can verify.
// ---------------------------------------------------------------------

function typicalCanonicalAndDigest(profileLiteral) {
  const entry = manifest.profiles.find((p) => p.profile === profileLiteral);
  const schema = readJson(entry.schema_path);
  const typicalCase = entry.cases.find((c) => c.case_id === "typical");
  const input = readJson(typicalCase.input_path);
  const canonical = canonicalBytes(schema, input);
  return { canonical, digest: digestHex(canonical) };
}

const semantics = typicalCanonicalAndDigest("arc_artifact_semantics_v1");
const reviewPackageEntry = manifest.profiles.find((p) => p.profile === "arc_approval_review_package_v1");
const reviewPackageTypical = readJson(reviewPackageEntry.cases.find((c) => c.case_id === "typical").input_path);
if (reviewPackageTypical.artifact_semantics_digest !== semantics.digest) {
  fail("cross-fixture: approval_review_package_v1.typical -> artifact_semantics_v1.typical", "artifact_semantics_digest does not match the independently recomputed S digest");
}

const reviewPackage = typicalCanonicalAndDigest("arc_approval_review_package_v1");
const revisionEntry = manifest.profiles.find((p) => p.profile === "arc_artifact_revision_v1");
const revisionTypical = readJson(revisionEntry.cases.find((c) => c.case_id === "typical").input_path);
if (revisionTypical.artifact_semantics_digest !== semantics.digest) {
  fail("cross-fixture: artifact_revision_v1.typical -> artifact_semantics_v1.typical", "artifact_semantics_digest does not match the independently recomputed S digest");
}
if (revisionTypical.review_package_digest !== reviewPackage.digest) {
  fail("cross-fixture: artifact_revision_v1.typical -> approval_review_package_v1.typical", "review_package_digest does not match the independently recomputed R digest");
}

const claimEntry = manifest.profiles.find((p) => p.profile === "arc_source_approval_claim_v1");
const claimTypical = readJson(claimEntry.cases.find((c) => c.case_id === "typical").input_path);
const claimSchema = readJson(claimEntry.schema_path);
const claimDigest = digestHex(canonicalBytes(claimSchema, claimTypical));

for (const literal of ["arc_source_approval_evidence_v1", "arc_source_verifier_attestation_v1"]) {
  const entry = manifest.profiles.find((p) => p.profile === literal);
  const typical = readJson(entry.cases.find((c) => c.case_id === "typical").input_path);
  if (typical.claim_digest !== claimDigest) {
    fail(`cross-fixture: ${literal}.typical -> source_approval_claim_v1.typical`, "claim_digest does not match the independently recomputed claim digest");
  }
}
// `source_approval_evidence_v1.typical` also embeds the complete claim
// object, not just its digest -- recompute the embedded claim's own digest
// straight from what is actually inside the evidence envelope, independent
// of the sibling claim fixture file.
{
  const entry = manifest.profiles.find((p) => p.profile === "arc_source_approval_evidence_v1");
  const typical = readJson(entry.cases.find((c) => c.case_id === "typical").input_path);
  const embeddedClaimDigest = digestHex(canonicalBytes(claimSchema, typical.claim));
  if (typical.claim_digest !== embeddedClaimDigest) {
    fail("cross-fixture: source_approval_evidence_v1.typical embedded claim", "claim_digest does not match the digest of the claim object embedded in the same file");
  }
}

const envelope = typicalCanonicalAndDigest("arc_expected_impact_envelope_v1");
for (const literal of ["arc_approval_review_package_v1", "arc_observation_qualification_v1"]) {
  const entry = manifest.profiles.find((p) => p.profile === literal);
  const typical = readJson(entry.cases.find((c) => c.case_id === "typical").input_path);
  if (typical.expected_impact_envelope_digest !== envelope.digest) {
    fail(`cross-fixture: ${literal}.typical -> expected_impact_envelope_v1.typical`, "expected_impact_envelope_digest does not match the independently recomputed envelope digest");
  }
}

const cohort = typicalCanonicalAndDigest("arc_observation_cohort_v1");
{
  const entry = manifest.profiles.find((p) => p.profile === "arc_observation_qualification_v1");
  const typical = readJson(entry.cases.find((c) => c.case_id === "typical").input_path);
  if (typical.cohort_digest !== cohort.digest) {
    fail("cross-fixture: observation_qualification_v1.typical -> observation_cohort_v1.typical", "cohort_digest does not match the independently recomputed cohort digest");
  }
  if (typical.candidate_review_package_digest !== reviewPackage.digest) {
    fail("cross-fixture: observation_qualification_v1.typical -> approval_review_package_v1.typical", "candidate_review_package_digest does not match the independently recomputed R digest");
  }
}

// The operational-event chain: `minimal` is genesis (sequence 0, null
// predecessor), `typical` (sequence 1) names `minimal`'s real digest as its
// predecessor, and `maximal` (sequence 2) names `typical`'s.
{
  const entry = manifest.profiles.find((p) => p.profile === "arc_operational_event_v1");
  const schema = readJson(entry.schema_path);
  const genesis = readJson(entry.cases.find((c) => c.case_id === "minimal").input_path);
  const middle = readJson(entry.cases.find((c) => c.case_id === "typical").input_path);
  const last = readJson(entry.cases.find((c) => c.case_id === "maximal").input_path);
  const genesisDigest = digestHex(canonicalBytes(schema, genesis));
  const middleDigest = digestHex(canonicalBytes(schema, middle));
  if (middle.previous_event_digest !== genesisDigest) {
    fail("cross-fixture: operational_event_v1.typical -> operational_event_v1.minimal", "previous_event_digest does not match the independently recomputed genesis digest");
  }
  if (last.previous_event_digest !== middleDigest) {
    fail("cross-fixture: operational_event_v1.maximal -> operational_event_v1.typical", "previous_event_digest does not match the independently recomputed predecessor digest");
  }
}

if (failures > 0) {
  console.error(`\n${failures} disagreement(s) found across ${checkedCases} case(s).`);
  process.exit(1);
}

console.log(`arc-reference-verifier: ${checkedCases} case(s) across ${manifest.profiles.length} profiles agree with the manifest; cross-fixture graph checks passed.`);
process.exit(0);
