declare interface ISearchEnhancementCommandSetStrings {
  EnhanceCommand: string;
  RetryCommand: string;
  RemoveCommand: string;
  EnhanceConfirmation: string;
  RemoveConfirmation: string;
  EnhanceQueued: string;
  RemoveQueued: string;
  InvalidSelection: string;
  LibraryUnavailable: string;
  UpdateFailed: string;
}

declare module 'SearchEnhancementCommandSetStrings' {
  const strings: ISearchEnhancementCommandSetStrings;
  export = strings;
}
