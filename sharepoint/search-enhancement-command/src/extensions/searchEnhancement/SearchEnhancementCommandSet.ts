import { Guid, Log } from '@microsoft/sp-core-library';
import { Dialog } from '@microsoft/sp-dialog';
import { SPPermission } from '@microsoft/sp-page-context';
import {
  BaseListViewCommandSet,
  type Command,
  type IListViewCommandSetExecuteEventParameters,
  type ListViewStateChangedEventArgs,
  type RowAccessor
} from '@microsoft/sp-listview-extensibility';
import {
  AadHttpClient,
  type AadHttpClientResponse,
  SPHttpClient,
  type SPHttpClientResponse
} from '@microsoft/sp-http';

import { commandVisibility, type SearchStatus } from './commandVisibility';
import * as strings from 'SearchEnhancementCommandSetStrings';

const LOG_SOURCE: string = 'SearchEnhancementCommandSet';
const ENHANCE_COMMAND: string = 'ENHANCE';
const REMOVE_COMMAND: string = 'REMOVE';
const DEFAULT_API_BASE_URL: string = 'https://REPLACE-WITH-WEB-APP.azurecontainerapps.io';
const DEFAULT_API_RESOURCE: string = 'api://REPLACE-WITH-API-APP-ID';
const STATUS_LINK_DESCRIPTION: string = '진행 상황 보기';

type SearchEnhancementCommand = 'Enhance' | 'Remove';
type SharePointFieldValue = string | IHyperlinkFieldValue;

interface ISearchEnhancementCommandSetProperties {
  apiBaseUrl?: string;
  apiResource?: string;
}

interface IHyperlinkFieldValue {
  Url: string;
  Description: string;
}

interface IIngestRequestItem {
  listItemId: string;
  fileName?: string;
}

interface IIngestRequestBody {
  command: SearchEnhancementCommand;
  requestId: string;
  item: IIngestRequestItem;
}

interface IIngestResponseBody {
  requestId: string;
  statusToken: string;
  statusUrl: string;
  status: string;
  jobType: string;
}

interface ISelectedPowerPoint {
  itemId: number;
  fileName: string;
  status: SearchStatus;
}

export default class SearchEnhancementCommandSet extends BaseListViewCommandSet<ISearchEnhancementCommandSetProperties> {
  private _executing: boolean = false;

  public onInit(): Promise<void> {
    Log.info(LOG_SOURCE, 'Initialized');
    this._setCommandVisibility(false, false);
    this.context.listView.listViewStateChangedEvent.add(
      this,
      this._onListViewStateChanged
    );
    return Promise.resolve();
  }

  public onExecute(event: IListViewCommandSetExecuteEventParameters): void {
    if (event.itemId === ENHANCE_COMMAND) {
      this._executeEnhance().catch((error: unknown) => {
        Log.error(LOG_SOURCE, this._normalizeError(error));
      });
      return;
    }
    if (event.itemId === REMOVE_COMMAND) {
      this._executeRemove().catch((error: unknown) => {
        Log.error(LOG_SOURCE, this._normalizeError(error));
      });
      return;
    }
    throw new Error(`Unknown command: ${event.itemId}`);
  }

  private _onListViewStateChanged = (
    _args: ListViewStateChangedEventArgs
  ): void => {
    const selection: ISelectedPowerPoint | undefined = this._selectedPowerPoint();
    const canEdit: boolean =
      !this._executing &&
      this.context.pageContext.list?.permissions.hasPermission(
        SPPermission.editListItems
      ) === true;
    const visibility = commandVisibility(
      selection?.fileName,
      selection?.status,
      canEdit
    );
    this._setCommandVisibility(visibility.enhance, visibility.remove);
    this.raiseOnChange();
  };

  private async _executeEnhance(): Promise<void> {
    const selection: ISelectedPowerPoint | undefined = this._selectedPowerPoint();
    if (!selection) {
      await Dialog.alert(strings.InvalidSelection);
      return;
    }
    const confirmed: boolean = window.confirm(
      `${selection.fileName}\n\n${strings.EnhanceConfirmation}`
    );
    if (!confirmed) {
      return;
    }
    await this._submitCommand(selection, 'Enhance', 'Queued');
    await Dialog.alert(strings.EnhanceQueued);
  }

  private async _executeRemove(): Promise<void> {
    const selection: ISelectedPowerPoint | undefined = this._selectedPowerPoint();
    if (!selection) {
      await Dialog.alert(strings.InvalidSelection);
      return;
    }
    const confirmed: boolean = window.confirm(
      `${selection.fileName}\n\n${strings.RemoveConfirmation}`
    );
    if (!confirmed) {
      return;
    }
    await this._submitCommand(selection, 'Remove', 'Removing');
    await Dialog.alert(strings.RemoveQueued);
  }

  private async _submitCommand(
    selection: ISelectedPowerPoint,
    command: SearchEnhancementCommand,
    queuedStatus: SearchStatus
  ): Promise<void> {
    this._executing = true;
    this._setCommandVisibility(false, false);
    this.raiseOnChange();
    try {
      const listId: string | undefined =
        this.context.pageContext.list?.id.toString();
      if (!listId) {
        throw new Error(strings.LibraryUnavailable);
      }

      const requestId: string = Guid.newGuid().toString();
      const ingestResponse: IIngestResponseBody = await this._enqueueRequest(
        selection,
        command,
        requestId
      );

      await this._mergeListItemFields(selection.itemId, listId, {
        CrewmealSearchRequestId: requestId,
        CrewmealSearchStatus: queuedStatus,
        CrewmealSearchStatusLink: {
          Url: ingestResponse.statusUrl,
          Description: STATUS_LINK_DESCRIPTION
        }
      });
      window.location.reload();
    } catch (error: unknown) {
      const normalized: Error = this._normalizeError(error);
      Log.error(LOG_SOURCE, normalized);
      await Dialog.alert(`${strings.UpdateFailed}\n${normalized.message}`);
      throw normalized;
    } finally {
      this._executing = false;
      this._onListViewStateChanged({} as ListViewStateChangedEventArgs);
    }
  }

  private async _enqueueRequest(
    selection: ISelectedPowerPoint,
    command: SearchEnhancementCommand,
    requestId: string
  ): Promise<IIngestResponseBody> {
    const endpoint: string = `${this._apiBaseUrl}/api/requests`;
    const client: AadHttpClient = await this.context.aadHttpClientFactory.getClient(
      this._apiResource
    );
    const requestBody: IIngestRequestBody = {
      command,
      requestId,
      item: {
        listItemId: selection.itemId.toString(),
        fileName: selection.fileName
      }
    };
    const response: AadHttpClientResponse = await client.post(
      endpoint,
      AadHttpClient.configurations.v1,
      {
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(requestBody)
      }
    );
    if (!response.ok || response.status !== 202) {
      throw new Error(
        `${strings.UpdateFailed} HTTP ${response.status} ${response.statusText}`
      );
    }
    const responseJson: unknown = await response.json();
    return this._parseIngestResponse(responseJson);
  }

  private async _mergeListItemFields(
    itemId: number,
    listId: string,
    fields: Record<string, SharePointFieldValue>
  ): Promise<void> {
    const endpoint: string =
      `${this.context.pageContext.web.absoluteUrl}` +
      `/_api/web/lists(guid'${listId}')/items(${itemId})`;
    const response: SPHttpClientResponse = await this.context.spHttpClient.post(
      endpoint,
      SPHttpClient.configurations.v1,
      {
        headers: {
          Accept: 'application/json;odata=nometadata',
          'Content-Type': 'application/json;odata=nometadata',
          'IF-MATCH': '*',
          'X-HTTP-Method': 'MERGE'
        },
        body: JSON.stringify(fields)
      }
    );
    if (!response.ok) {
      throw new Error(
        `${strings.UpdateFailed} HTTP ${response.status} ${response.statusText}`
      );
    }
  }

  private _parseIngestResponse(responseJson: unknown): IIngestResponseBody {
    if (!this._isRecord(responseJson)) {
      throw new Error('The ingest API response was not a JSON object.');
    }
    const statusUrl: unknown = responseJson.statusUrl;
    if (typeof statusUrl !== 'string' || statusUrl.length === 0) {
      throw new Error('The ingest API response did not include statusUrl.');
    }
    return {
      requestId: this._readString(responseJson, 'requestId'),
      statusToken: this._readString(responseJson, 'statusToken'),
      statusUrl,
      status: this._readString(responseJson, 'status'),
      jobType: this._readString(responseJson, 'jobType')
    };
  }

  private _readString(
    source: Record<string, unknown>,
    propertyName: string
  ): string {
    const value: unknown = source[propertyName];
    return typeof value === 'string' ? value : '';
  }

  private _isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null;
  }

  private get _apiBaseUrl(): string {
    return (this.properties.apiBaseUrl || DEFAULT_API_BASE_URL)
      .trim()
      .replace(/\/+$/, '');
  }

  private get _apiResource(): string {
    return (this.properties.apiResource || DEFAULT_API_RESOURCE).trim();
  }

  private _selectedPowerPoint(): ISelectedPowerPoint | undefined {
    const rows: readonly RowAccessor[] | undefined =
      this.context.listView.selectedRows;
    if (!rows || rows.length !== 1) {
      return undefined;
    }
    const row: RowAccessor = rows[0];
    const rawId: unknown = row.getValueByName('ID');
    const itemId: number = Number(rawId);
    const fileName: string = String(
      row.getValueByName('FileLeafRef') ?? ''
    ).trim();
    const status: SearchStatus = String(
      row.getValueByName('CrewmealSearchStatus') ?? ''
    ).trim() as SearchStatus;
    if (!Number.isInteger(itemId) || itemId <= 0 || !fileName) {
      return undefined;
    }
    return { itemId, fileName, status };
  }

  private _setCommandVisibility(enhance: boolean, remove: boolean): void {
    const enhanceCommand: Command | undefined =
      this.tryGetCommand(ENHANCE_COMMAND);
    const removeCommand: Command | undefined =
      this.tryGetCommand(REMOVE_COMMAND);
    if (enhanceCommand) {
      enhanceCommand.visible = enhance;
      enhanceCommand.title =
        this._selectedPowerPoint()?.status === 'Failed' ||
        this._selectedPowerPoint()?.status === 'Stale'
          ? strings.RetryCommand
          : strings.EnhanceCommand;
    }
    if (removeCommand) {
      removeCommand.visible = remove;
    }
  }

  private _normalizeError(error: unknown): Error {
    return error instanceof Error ? error : new Error(String(error));
  }
}
