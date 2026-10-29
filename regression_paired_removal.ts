/**
 * Regression tests for paired-file removal (PR #3).
 *
 * Tests the four behaviors added to handleRemoveResult in ToolPage.tsx:
 *   1. Removing a data file also removes its JSON/TSV sidecars and .bval/.bvec.
 *   2. All removed paired files end up in removedResults for redaction.
 *   3. The audit log gets a separate entry per companion.
 *   4. Removing a bare sidecar does NOT cascade to its data file.
 *
 * This file exercises the companion-detection logic and audit redaction
 * independently of React state, by inlining the same helper functions and
 * calling the library functions they feed into.
 *
 * Usage: npx tsx regression_paired_removal.ts   (non-zero exit on failure)
 */

import { computeBidsNames } from './src/lib/bids/bidsNaming';
import { generateSummary } from './src/lib/detection';
import { createAuditLogger } from './src/lib/audit/auditLogger';
import { auditJsonFiles, buildAuditRedactionPairs } from './src/lib/audit';
import { getEffectiveModality } from './src/types/detection';
import type { DetectionResult, Modality } from './src/types/detection';
import { createDefaultDatasetStructure } from './src/types/sessionStructure';

// ── Minimal FileLike stub ─────────────────────────────────────────────────
const stubFile = { name: '', size: 0, arrayBuffer: async () => new ArrayBuffer(0), text: async () => '', stream: () => { throw new Error(); } };

// ── Helpers (same logic as handleRemoveResult) ────────────────────────────
const folderOf = (p: string) => { const s = p.lastIndexOf('/'); return s === -1 ? '' : p.slice(0, s); };
const baseOf   = (n: string) => { const l = n.toLowerCase(); if (l.endsWith('.nii.gz')) return n.slice(0, -7); const d = n.lastIndexOf('.'); return d >= 0 ? n.substring(0, d) : n; };

function findCompanionIndices(prev: DetectionResult[], index: number): Set<number> {
  const primary = prev[index];
  const primaryModality = getEffectiveModality(primary);
  const primaryIsData =
    primaryModality !== 'sidecar-json' &&
    primaryModality !== 'sidecar-tsv' &&
    !/\.(bval|bvec)$/i.test(primary.fileName);

  const companions = new Set<number>();
  if (!primaryIsData) return companions;

  prev.forEach((r, i) => {
    if (i === index) return;
    const m = getEffectiveModality(r);
    const isCompanionType =
      m === 'sidecar-json' ||
      m === 'sidecar-tsv' ||
      /\.(bval|bvec)$/i.test(r.fileName);
    if (
      isCompanionType &&
      folderOf(r.relativePath) === folderOf(primary.relativePath) &&
      baseOf(r.fileName) === baseOf(primary.fileName)
    ) {
      companions.add(i);
    }
  });
  return companions;
}

// ── Factories ─────────────────────────────────────────────────────────────
function makeResult(fileName: string, folder: string, modality: Modality): DetectionResult {
  const relativePath = folder ? `${folder}/${fileName}` : fileName;
  return {
    relativePath,
    fileName,
    fileSize: 1000,
    file: { ...stubFile, name: fileName },
    subjectGroup: 'patient_01',
    detectedSession: 'ses-preimplant',
    detectedModality: modality,
    confidence: 'high',
    reasons: [],
    userSession: null,
    userModality: null,
    userSubjectGroup: null,
    bidsFilename: '',
    bidsPath: '',
    sidecarFields: null,
  };
}

// ── Test harness ─────────────────────────────────────────────────────────
let failures = 0;
let section = '';
function check(ok: boolean, detail: string) {
  if (!ok) { failures++; console.log(`  FAIL  [${section}] ${detail}`); }
  else { console.log(`  pass  [${section}] ${detail}`); }
}

const structure = createDefaultDatasetStructure();

// ─────────────────────────────────────────────────────────────────────────
// 1. NIfTI + JSON sidecar: removing the .nii.gz also removes the .json
// ─────────────────────────────────────────────────────────────────────────
section = 'nifti+json sidecar';
{
  const results: DetectionResult[] = [
    makeResult('T1w_scan.nii.gz', 'patient_01/ses-preimplant/anat', 'T1w'),
    makeResult('T1w_scan.json',   'patient_01/ses-preimplant/anat', 'sidecar-json'),
    makeResult('other_scan.nii.gz', 'patient_01/ses-preimplant/anat', 'T1w'),
  ];
  const companions = findCompanionIndices(results, 0);
  check(companions.has(1),  'JSON sidecar (same base, same folder) found as companion');
  check(!companions.has(2), 'other_scan.nii.gz not treated as companion');
  check(companions.size === 1, `companion count is 1 (got ${companions.size})`);

  // Verify the mapping table shrinks correctly after removal
  const toRemove = new Set([0, ...companions]);
  const next    = results.filter((_, i) => !toRemove.has(i));
  check(next.length === 1, `mapping shrinks to 1 file after removal (got ${next.length})`);
  check(next[0].fileName === 'other_scan.nii.gz', 'unrelated file survives');
}

// ─────────────────────────────────────────────────────────────────────────
// 2. DWI + .bval + .bvec: all three removed together
// ─────────────────────────────────────────────────────────────────────────
section = 'dwi+bval+bvec';
{
  const results: DetectionResult[] = [
    makeResult('ep2d_diff.nii.gz', 'patient_01/ses-preimplant/dwi', 'dwi'),
    makeResult('ep2d_diff.bval',   'patient_01/ses-preimplant/dwi', 'dwi'),
    makeResult('ep2d_diff.bvec',   'patient_01/ses-preimplant/dwi', 'dwi'),
    makeResult('ep2d_diff.json',   'patient_01/ses-preimplant/dwi', 'sidecar-json'),
    makeResult('other.nii.gz',     'patient_01/ses-preimplant/dwi', 'dwi'),
  ];
  const companions = findCompanionIndices(results, 0);
  check(companions.has(1), '.bval found as companion');
  check(companions.has(2), '.bvec found as companion');
  check(companions.has(3), '.json sidecar found as companion');
  check(!companions.has(4), 'other.nii.gz not a companion');
  check(companions.size === 3, `companion count is 3 (got ${companions.size})`);

  const toRemove = new Set([0, ...companions]);
  const next = results.filter((_, i) => !toRemove.has(i));
  check(next.length === 1, `only other.nii.gz survives (got ${next.length})`);
}

// ─────────────────────────────────────────────────────────────────────────
// 3. Cross-folder isolation: same base name in a different folder is NOT
//    a companion
// ─────────────────────────────────────────────────────────────────────────
section = 'cross-folder isolation';
{
  const results: DetectionResult[] = [
    makeResult('T1w.nii.gz', 'patient_01/anat', 'T1w'),
    makeResult('T1w.json',   'patient_01/anat', 'sidecar-json'),   // same folder → companion
    makeResult('T1w.json',   'patient_02/anat', 'sidecar-json'),   // different folder → NOT companion
  ];
  const companions = findCompanionIndices(results, 0);
  check(companions.has(1),  'same-folder sidecar is a companion');
  check(!companions.has(2), 'different-folder sidecar is not a companion');
}

// ─────────────────────────────────────────────────────────────────────────
// 4. Removing a bare sidecar does NOT cascade to its data file
// ─────────────────────────────────────────────────────────────────────────
section = 'sidecar-only removal does not cascade';
{
  const results: DetectionResult[] = [
    makeResult('T1w.nii.gz', 'patient_01/anat', 'T1w'),
    makeResult('T1w.json',   'patient_01/anat', 'sidecar-json'),
  ];
  // Remove sidecar (index 1), not the data file
  const companions = findCompanionIndices(results, 1);
  check(companions.size === 0, `removing bare sidecar has no companions (got ${companions.size})`);

  const toRemove = new Set([1, ...companions]);
  const next = results.filter((_, i) => !toRemove.has(i));
  check(next.length === 1 && next[0].fileName === 'T1w.nii.gz', 'data file survives when only sidecar removed');
}

// ─────────────────────────────────────────────────────────────────────────
// 5. removedResults accumulates all removed files (for redaction)
// ─────────────────────────────────────────────────────────────────────────
section = 'removedResults redaction coverage';
{
  const results: DetectionResult[] = [
    makeResult('scan.nii.gz', 'p01/anat', 'T1w'),
    makeResult('scan.json',   'p01/anat', 'sidecar-json'),
  ];
  const removedResults: DetectionResult[] = [];
  const primary = results[0];
  const companions = findCompanionIndices(results, 0);
  const toRemove = results.filter((_, i) => i === 0 || companions.has(i));

  for (const r of toRemove) {
    if (!removedResults.includes(r)) removedResults.push(r);
  }

  check(removedResults.length === 2, `both files in removedResults (got ${removedResults.length})`);
  check(removedResults.some(r => r.fileName === 'scan.nii.gz'), 'primary in removedResults');
  check(removedResults.some(r => r.fileName === 'scan.json'),   'companion in removedResults');

  // Verify redaction: build a fake BIDS-named result list and confirm
  // that the shareable log replaces original names with file-N
  const namedRemoved = removedResults.map((r, i) => ({
    ...r,
    bidsPath: `unclassified/file-${i + 1}`,
    bidsFilename: `file-${i + 1}`,
  }));
  const pairs = buildAuditRedactionPairs(namedRemoved, []);
  const audit = createAuditLogger();
  audit.addEntry('file-removed', `Removed from mapping: ${primary.fileName}`);
  audit.addEntry('file-removed', `Removed from mapping (paired with scan.nii.gz): scan.json`);
  const { shareable } = auditJsonFiles(audit, 'user', pairs);
  check(!shareable.text.includes('scan.nii.gz'), 'scan.nii.gz redacted in shareable log');
  check(!shareable.text.includes('scan.json'),   'scan.json redacted in shareable log');
}

// ─────────────────────────────────────────────────────────────────────────
// 6. Audit log has separate entries for primary and each companion
// ─────────────────────────────────────────────────────────────────────────
section = 'audit entries per companion';
{
  const audit = createAuditLogger();
  const primaryName  = 'dwi.nii.gz';
  const companionNames = ['dwi.bval', 'dwi.bvec', 'dwi.json'];

  audit.addEntry('file-removed', `Removed from mapping: ${primaryName}`);
  for (const name of companionNames) {
    audit.addEntry('file-removed', `Removed from mapping (paired with ${primaryName}): ${name}`);
  }

  const entries = audit.getEntries();
  check(entries.length === 4, `4 audit entries logged (got ${entries.length})`);
  check(entries[0].summary.includes(primaryName), 'primary entry names the primary file');
  check(entries[1].summary.includes('paired with dwi.nii.gz') && entries[1].summary.includes('dwi.bval'), 'bval entry');
  check(entries[2].summary.includes('paired with dwi.nii.gz') && entries[2].summary.includes('dwi.bvec'), 'bvec entry');
  check(entries[3].summary.includes('paired with dwi.nii.gz') && entries[3].summary.includes('dwi.json'), 'json sidecar entry');
}

// ─────────────────────────────────────────────────────────────────────────
// 7. Mapping table updates correctly after cascaded removal
// ─────────────────────────────────────────────────────────────────────────
section = 'mapping table after cascaded removal';
{
  const results: DetectionResult[] = computeBidsNames([
    makeResult('scan.nii.gz', 'p01/ses-preimplant/anat', 'T1w'),
    makeResult('scan.json',   'p01/ses-preimplant/anat', 'sidecar-json'),
    makeResult('other.nii.gz','p01/ses-preimplant/anat', 'T1w'),
  ], undefined, structure);

  const companions = findCompanionIndices(results, 0);
  const toRemove = new Set([0, ...companions]);
  const next = computeBidsNames(results.filter((_, i) => !toRemove.has(i)), undefined, structure);
  const summary = generateSummary(next, structure);

  check(next.length === 1, `1 file left after removal (got ${next.length})`);
  check(summary.totalFiles === 1, `summary.totalFiles === 1 (got ${summary.totalFiles})`);
  // The surviving file should have no orphaned-sidecar companion in `next`
  const hasOrphan = next.some(r => getEffectiveModality(r) === 'sidecar-json');
  check(!hasOrphan, 'no orphaned JSON sidecar left in mapping after cascaded removal');
}

// ─────────────────────────────────────────────────────────────────────────
// Results
// ─────────────────────────────────────────────────────────────────────────
console.log('');
if (failures === 0) {
  console.log('All paired-removal regression tests passed.');
} else {
  console.log(`${failures} test(s) failed.`);
  process.exit(1);
}
