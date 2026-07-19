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

export function commandVisibility(
  fileName: string | undefined,
  status: SearchStatus | undefined,
  canEdit: boolean
): ICommandVisibility {
  if (!canEdit || !fileName?.toLowerCase().endsWith('.pptx')) {
    return { enhance: false, remove: false };
  }
  const effectiveStatus: SearchStatus = status ?? '';
  return {
    enhance:
      ['', 'NotEnabled', 'Stale', 'Failed'].indexOf(effectiveStatus) >= 0,
    remove: ['Queued', 'Processing', 'Ready'].indexOf(effectiveStatus) >= 0
  };
}
