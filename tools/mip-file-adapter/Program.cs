/*
 * CrewMeal MIP File SDK adapter.
 *
 * A thin, headless command-line wrapper around the official Microsoft
 * Information Protection (MIP) File SDK that satisfies the CrewMeal decryption
 * seam contract (see src/crewmeal/search_enhancement/mip_sdk.py):
 *
 *     crewmeal-mip-adapter unprotect --in <input> --out <output> --token-file <token>
 *
 *   * exit 0  -> success; decrypted (unprotected) bytes written to <output>.
 *   * nonzero -> failure; stderr explains why.
 *
 * Authentication is unattended and app-only. The MIP File SDK engine challenges
 * for several resources in turn -- the MIP Sync Service (label policy) and the
 * Azure Rights Management service (decryption) -- so this adapter mints an
 * app-only token per requested resource using the M365 service principal's
 * client credentials (CREWMEAL_M365_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET).
 * The service principal must hold the Azure RMS super-user right
 * (Content.SuperUser) so it can decrypt any protected content in the tenant
 * regardless of the document's rights policy. If no client secret is available,
 * the adapter falls back to the bearer token supplied via --token-file (an RMS
 * token pre-acquired by CrewMeal). No interactive login.
 */

using Microsoft.Identity.Client;
using Microsoft.InformationProtection;
using Microsoft.InformationProtection.Exceptions;
using Microsoft.InformationProtection.File;

namespace CrewMeal.Mip.Adapter;

internal static class ExitCodes
{
    public const int Success = 0;
    public const int UsageError = 2;
    public const int AccessDenied = 3;
    public const int OperationFailed = 4;
    public const int Unexpected = 5;
}

internal sealed class AdapterOptions
{
    public string Subcommand = "unprotect";
    public string? InputPath;
    public string? OutputPath;
    public string? TokenFile;
    public string? ClientId;
    public string? Identity;
    public string? ActualName;

    public static AdapterOptions Parse(string[] args)
    {
        var options = new AdapterOptions();
        var positional = new List<string>();

        for (var i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            switch (arg)
            {
                case "--in":
                    options.InputPath = RequireValue(args, ref i, arg);
                    break;
                case "--out":
                    options.OutputPath = RequireValue(args, ref i, arg);
                    break;
                case "--token-file":
                    options.TokenFile = RequireValue(args, ref i, arg);
                    break;
                case "--client-id":
                    options.ClientId = RequireValue(args, ref i, arg);
                    break;
                case "--identity":
                    options.Identity = RequireValue(args, ref i, arg);
                    break;
                case "--name":
                case "--actual-name":
                    options.ActualName = RequireValue(args, ref i, arg);
                    break;
                case "-h":
                case "--help":
                    throw new UsageException(Usage);
                default:
                    if (arg.StartsWith('-'))
                    {
                        throw new UsageException($"Unknown option: {arg}\n\n{Usage}");
                    }
                    positional.Add(arg);
                    break;
            }
        }

        if (positional.Count > 0)
        {
            options.Subcommand = positional[0].ToLowerInvariant();
        }

        return options;
    }

    private static string RequireValue(string[] args, ref int i, string name)
    {
        if (i + 1 >= args.Length)
        {
            throw new UsageException($"Option {name} requires a value.\n\n{Usage}");
        }
        return args[++i];
    }

    public const string Usage =
        "Usage: crewmeal-mip-adapter unprotect --in <input> --out <output> "
        + "--token-file <token> [--client-id <guid>] [--identity <upn>] [--name <original-filename>]";
}

internal sealed class UsageException : Exception
{
    public UsageException(string message) : base(message) { }
}

/// <summary>
/// Acquires an app-only token for whatever resource the MIP SDK challenges for.
/// The File SDK asks for the MIP Sync Service resource (label policy) during
/// engine creation and the Azure RMS resource during decryption, so a single
/// pre-acquired token is not enough -- the delegate mints one per resource via
/// client credentials, caching by resource. When no client secret is available
/// it falls back to a single pre-acquired bearer token (RMS only).
/// </summary>
internal sealed class AppOnlyAuthDelegate : IAuthDelegate
{
    private readonly IConfidentialClientApplication? _app;
    private readonly string? _fallbackToken;
    private readonly object _lock = new();
    private readonly Dictionary<string, string> _cache = new(StringComparer.OrdinalIgnoreCase);

    public AppOnlyAuthDelegate(string? tenantId, string clientId, string? clientSecret, string? fallbackToken)
    {
        _fallbackToken = fallbackToken;
        if (!string.IsNullOrWhiteSpace(clientSecret) && !string.IsNullOrWhiteSpace(tenantId))
        {
            _app = ConfidentialClientApplicationBuilder.Create(clientId)
                .WithClientSecret(clientSecret)
                .WithAuthority(new Uri($"https://login.microsoftonline.com/{tenantId}"))
                .Build();
        }
    }

    public bool CanMintTokens => _app is not null;

    public string AcquireToken(Identity identity, string authority, string resource, string claims)
    {
        // Logged to stderr (never stdout) to aid debugging of multi-resource
        // challenges without leaking the token itself.
        Console.Error.WriteLine(
            $"[adapter] token requested: authority={authority} resource={resource}");

        var resourceKey = resource.TrimEnd('/');

        if (_app is not null)
        {
            lock (_lock)
            {
                if (_cache.TryGetValue(resourceKey, out var cached))
                {
                    return cached;
                }
            }

            // The SDK hands us "https://login.windows.net/common"; app-only tokens
            // must target the tenant, which the confidential client is already
            // pinned to. The resource maps to a "<resource>/.default" scope.
            var scope = resourceKey + "/.default";
            var result = _app.AcquireTokenForClient(new[] { scope })
                .ExecuteAsync()
                .GetAwaiter()
                .GetResult();

            lock (_lock)
            {
                _cache[resourceKey] = result.AccessToken;
            }
            return result.AccessToken;
        }

        if (!string.IsNullOrWhiteSpace(_fallbackToken))
        {
            return _fallbackToken;
        }

        throw new InvalidOperationException(
            $"No credentials available to acquire a token for {resource}. Provide "
            + "CREWMEAL_M365_CLIENT_SECRET (+ _TENANT_ID) or a valid --token-file.");
    }
}

/// <summary>Auto-accepts consent prompts; there is no interactive user.</summary>
internal sealed class AutoConsentDelegate : IConsentDelegate
{
    public Consent GetUserConsent(string url) => Consent.AcceptAlways;
}

internal static class Program
{
    private const string ApplicationName = "CrewMeal MIP Adapter";
    private const string ApplicationVersion = "1.0.0";

    private static async Task<int> Main(string[] args)
    {
        AdapterOptions options;
        try
        {
            options = AdapterOptions.Parse(args);
        }
        catch (UsageException ex)
        {
            Console.Error.WriteLine(ex.Message);
            return ExitCodes.UsageError;
        }

        try
        {
            switch (options.Subcommand)
            {
                case "unprotect":
                    return await UnprotectAsync(options);
                default:
                    Console.Error.WriteLine(
                        $"Unsupported subcommand: {options.Subcommand}\n\n{AdapterOptions.Usage}");
                    return ExitCodes.UsageError;
            }
        }
        catch (UsageException ex)
        {
            Console.Error.WriteLine(ex.Message);
            return ExitCodes.UsageError;
        }
        catch (AccessDeniedException ex)
        {
            Console.Error.WriteLine(
                "[adapter] access denied by Azure RMS. The service principal likely "
                + "lacks the Content.SuperUser right, or super-user is not enabled for "
                + $"the tenant. Details: {ex.Message}");
            return ExitCodes.AccessDenied;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[adapter] {ex.GetType().Name}: {ex.Message}");
            if (ex.InnerException is not null)
            {
                Console.Error.WriteLine($"[adapter] inner: {ex.InnerException.Message}");
            }
            return ExitCodes.Unexpected;
        }
    }

    private static async Task<int> UnprotectAsync(AdapterOptions options)
    {
        var inputPath = RequirePath(options.InputPath, "--in", mustExist: true);
        var outputPath = RequirePath(options.OutputPath, "--out", mustExist: false);

        var clientId = options.ClientId
            ?? Environment.GetEnvironmentVariable("CREWMEAL_M365_CLIENT_ID");
        if (string.IsNullOrWhiteSpace(clientId))
        {
            throw new UsageException(
                "Missing application (client) id. Pass --client-id or set "
                + "CREWMEAL_M365_CLIENT_ID. It must match the Entra app registration "
                + "whose service principal holds the RMS super-user right.");
        }

        var tenantId = Environment.GetEnvironmentVariable("CREWMEAL_M365_TENANT_ID");
        var clientSecret = Environment.GetEnvironmentVariable("CREWMEAL_M365_CLIENT_SECRET");

        var identityEmail = options.Identity
            ?? Environment.GetEnvironmentVariable("CREWMEAL_MIP_ENGINE_IDENTITY");

        // --token-file is an optional fallback (a pre-acquired RMS bearer). It is
        // only sufficient on its own for the RMS challenge; the sync-service
        // challenge during engine creation needs client credentials.
        string? fallbackToken = null;
        if (!string.IsNullOrWhiteSpace(options.TokenFile) && File.Exists(options.TokenFile))
        {
            var contents = (await File.ReadAllTextAsync(options.TokenFile)).Trim();
            if (contents.Length > 0)
            {
                fallbackToken = contents;
            }
        }

        var authDelegate = new AppOnlyAuthDelegate(tenantId, clientId, clientSecret, fallbackToken);
        if (!authDelegate.CanMintTokens && fallbackToken is null)
        {
            throw new UsageException(
                "No credentials available. Set CREWMEAL_M365_CLIENT_SECRET (and "
                + "CREWMEAL_M365_TENANT_ID) so the adapter can mint per-resource "
                + "app-only tokens, or pass a valid --token-file.");
        }
        if (!authDelegate.CanMintTokens)
        {
            Console.Error.WriteLine(
                "[adapter] warning: no client secret; using --token-file fallback. "
                + "Engine creation may fail if the SDK challenges for a non-RMS resource.");
        }

        var appInfo = new ApplicationInfo
        {
            ApplicationId = clientId,
            ApplicationName = ApplicationName,
            ApplicationVersion = ApplicationVersion,
        };

        // Load native SDK binaries. Throws if the runtime libs are missing.
        MIP.Initialize(MipComponent.File);

        var mipDataPath = Path.Combine(Path.GetTempPath(), "crewmeal-mip-data");
        var mipConfiguration = new MipConfiguration(
            appInfo, mipDataPath, Microsoft.InformationProtection.LogLevel.Error, false);
        var mipContext = MIP.CreateMipContext(mipConfiguration);

        IFileProfile? profile = null;
        IFileEngine? engine = null;
        try
        {
            // In-memory cache keeps this subprocess stateless: no on-disk profile
            // state to clean up between invocations.
            var profileSettings = new FileProfileSettings(
                mipContext, CacheStorageType.InMemory, new AutoConsentDelegate());
            profile = await MIP.LoadFileProfileAsync(profileSettings);


            var engineId = string.IsNullOrWhiteSpace(identityEmail)
                ? "crewmeal-mip-adapter"
                : identityEmail!;
            var engineSettings = new FileEngineSettings(engineId, authDelegate, string.Empty, "en-US");

            // A file engine requires either an Identity or a Cloud. App-only auth
            // has no user identity, so pin the sovereign cloud (default Commercial;
            // override via CREWMEAL_MIP_CLOUD, e.g. GccHigh / Dod).
            engineSettings.Cloud = ResolveCloud();

            if (!string.IsNullOrWhiteSpace(identityEmail))
            {
                // Aids RMS service/region discovery; optional for super-user
                // consumption because the file's publishing license carries the
                // licensing URL.
                engineSettings.Identity = new Identity(identityEmail);
            }
            engine = await profile.AddEngineAsync(engineSettings);

            var actualName = string.IsNullOrWhiteSpace(options.ActualName)
                ? inputPath
                : options.ActualName!;
            var handler = await engine.CreateFileHandlerAsync(inputPath, actualName, false);

            if (handler.Protection is null)
            {
                // Not actually protected: pass the bytes through unchanged so the
                // pipeline can continue. Detection upstream may have been broad.
                Console.Error.WriteLine(
                    "[adapter] input is not protected; copying through unchanged.");
                File.Copy(inputPath, outputPath, overwrite: true);
                return ExitCodes.Success;
            }

            Console.Error.WriteLine(
                "[adapter] protected content detected; removing protection as super-user.");
            handler.RemoveProtection();

            var committed = await handler.CommitAsync(outputPath);
            if (!committed)
            {
                Console.Error.WriteLine(
                    "[adapter] CommitAsync reported no changes written; unprotect failed.");
                SafeDelete(outputPath);
                return ExitCodes.OperationFailed;
            }

            if (!File.Exists(outputPath) || new FileInfo(outputPath).Length == 0)
            {
                Console.Error.WriteLine("[adapter] output file missing or empty after commit.");
                return ExitCodes.OperationFailed;
            }

            Console.Error.WriteLine("[adapter] unprotect succeeded.");
            return ExitCodes.Success;
        }
        finally
        {
            if (profile is not null && engine is not null)
            {
                try
                {
                    await profile.UnloadEngineAsync(engine.Settings.EngineId);
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[adapter] engine unload warning: {ex.Message}");
                }
            }
            mipContext.ShutDown();
        }
    }

    private static string RequirePath(string? value, string name, bool mustExist)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new UsageException($"Missing required option {name}.\n\n{AdapterOptions.Usage}");
        }
        if (mustExist && !File.Exists(value))
        {
            throw new UsageException($"File for {name} does not exist: {value}");
        }
        return value;
    }

    private static Cloud ResolveCloud()
    {
        var name = Environment.GetEnvironmentVariable("CREWMEAL_MIP_CLOUD");
        if (!string.IsNullOrWhiteSpace(name)
            && Enum.TryParse<Cloud>(name, ignoreCase: true, out var parsed))
        {
            return parsed;
        }
        return Cloud.Commercial;
    }

    private static void SafeDelete(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
            // best effort
        }
    }
}
