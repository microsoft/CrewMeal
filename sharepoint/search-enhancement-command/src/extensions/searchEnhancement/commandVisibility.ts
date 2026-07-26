export type SearchStatus =
  | ''
  | 'NotEnabled'
  | 'Queued'
  | 'Processing'
  | 'Ready'
  | 'Stale'
  | 'Removing'
  | 'Failed';

export interface ICommandVisibility {
  enhance: boolean;
  remove: boolean;
}

/**
 * File types the worker can process, mirroring the Python format registry
 * (`crewmeal.search_enhancement.formats.supported_extensions`).
 *
 * The client cannot see which formats an administrator has toggled off, so this
 * list is deliberately permissive: the worker re-checks every queued file
 * against `enabled_extensions` before ingesting it. Over-offering the command is
 * therefore safe, while under-offering it (as the previous `.pptx`-only check
 * did) silently hides the feature for supported formats.
 */
export const SUPPORTED_EXTENSIONS: string[] = [
  '.pptx',
  '.pdf',
  '.hwp',
  '.hwpx',
  '.docx',
  '.docm'
];

export function isSupportedFile(fileName: string | undefined): boolean {
  if (!fileName) {
    return false;
  }
  const lowered = fileName.toLowerCase();
  return SUPPORTED_EXTENSIONS.some(
    (extension) => lowered.endsWith(extension) && lowered.length > extension.length
  );
}

export function commandVisibility(
  fileName: string | undefined,
  status: SearchStatus | undefined,
  canEdit: boolean
): ICommandVisibility {
  if (!canEdit || !isSupportedFile(fileName)) {
    return { enhance: false, remove: false };
  }
  const effectiveStatus: SearchStatus = status ?? '';
  return {
    enhance:
      ['', 'NotEnabled', 'Stale', 'Failed'].indexOf(effectiveStatus) >= 0,
    remove: ['Queued', 'Processing', 'Ready'].indexOf(effectiveStatus) >= 0
  };
}
