/* AME Tech : lanceur portable (dérivé du lanceur Badaboum).
 *
 * Lance "<racine>\runtime\python\pythonw.exe -I -m ametech" sans console.
 * <racine> = %LOCALAPPDATA%\AME Tech\versions\<courante.txt> si ce dossier est
 * complet et au moins aussi récent que la version livrée avec ce lanceur
 * (version.txt à côté de l'exe), sinon le dossier du lanceur lui-même.
 *
 * Code de sortie 75 : relance immédiate (mise à jour ou retour arrière), la
 * racine est recalculée à chaque relance. Autre code non nul : relance
 * progressive, au plus 5 fois. Un second lancement délègue à
 * "pythonw -m ametech" (instance unique gérée par l'application).
 *
 * Aucun installateur, registre, service ni droit administrateur.
 * Compilation : voir build_launcher.py (zig cc, cible x86_64-windows-gnu).
 */
#ifndef UNICODE
#define UNICODE 1
#endif
#ifndef _UNICODE
#define _UNICODE 1
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>
#include <stdio.h>
#include <string.h>

#define PATH_CAP 32768
#define COMMAND_CAP 32768
#define STABLE_MS 300000ULL
#define MAX_CRASHES 5
#define CODE_RELANCE 75
#define MAX_RELANCES_RAPIDES 10
#define RELANCE_RAPIDE_MS 600000ULL

static HANDLE log_handle = INVALID_HANDLE_VALUE;
static wchar_t data_dir[PATH_CAP];   /* %LOCALAPPDATA%\AME Tech */
static wchar_t exe_path[PATH_CAP];
static wchar_t bundle_dir[PATH_CAP];

static int copy_w(wchar_t *out, size_t cap, const wchar_t *input) {
    size_t n = wcslen(input);
    if (n >= cap) return 0;
    memcpy(out, input, (n + 1) * sizeof(wchar_t));
    return 1;
}

static int append_w(wchar_t *out, size_t cap, const wchar_t *input) {
    size_t used = wcslen(out), n = wcslen(input);
    if (used + n >= cap) return 0;
    memcpy(out + used, input, (n + 1) * sizeof(wchar_t));
    return 1;
}

static int join_w(wchar_t *out, const wchar_t *base, const wchar_t *relative) {
    return copy_w(out, PATH_CAP, base) && append_w(out, PATH_CAP, L"\\") &&
           append_w(out, PATH_CAP, relative);
}

static int is_file(const wchar_t *name) {
    DWORD attrs = GetFileAttributesW(name);
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

static void message(const wchar_t *text) {
    MessageBoxW(NULL, text, L"AME Tech", MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND);
}

static void log_event(const char *label, DWORD code) {
    if (log_handle == INVALID_HANDLE_VALUE) return;
    SYSTEMTIME now;
    char buffer[256];
    DWORD written;
    GetLocalTime(&now);
    int n = snprintf(buffer, sizeof(buffer), "%04u-%02u-%02u %02u:%02u:%02u lanceur: %s (%lu)\r\n",
                     now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond,
                     label, (unsigned long) code);
    if (n > 0) WriteFile(log_handle, buffer, (DWORD)n, &written, NULL);
}

static void init_data_dir(void) {
    DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", data_dir, PATH_CAP);
    if (n == 0 || n >= PATH_CAP - 64) { data_dir[0] = 0; return; }
    append_w(data_dir, PATH_CAP, L"\\AME Tech");
    CreateDirectoryW(data_dir, NULL);
}

static void init_log(void) {
    wchar_t file[PATH_CAP], rotated[PATH_CAP];
    if (!data_dir[0]) return;
    if (!join_w(file, data_dir, L"lanceur.log") || !join_w(rotated, data_dir, L"lanceur.precedent.log")) return;
    WIN32_FILE_ATTRIBUTE_DATA attributes;
    if (GetFileAttributesExW(file, GetFileExInfoStandard, &attributes) &&
        (attributes.nFileSizeHigh > 0 || attributes.nFileSizeLow > 1024 * 1024)) {
        MoveFileExW(file, rotated, MOVEFILE_REPLACE_EXISTING);
    }
    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    log_handle = CreateFileW(file, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                             &sa, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

/* Lit au plus cap-1 octets d'un petit fichier texte, sans espaces finaux. */
static int read_small(const wchar_t *path, char *out, DWORD cap) {
    DWORD n = 0;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return 0;
    int ok = ReadFile(file, out, cap - 1, &n, NULL);
    CloseHandle(file);
    if (!ok) return 0;
    out[n] = 0;
    while (n > 0 && (out[n-1] == '\n' || out[n-1] == '\r' || out[n-1] == ' ' || out[n-1] == '\t')) out[--n] = 0;
    return n > 0;
}

/* Version stricte X.Y.Z (chiffres seulement : aucun chemin ne peut s'y glisser). */
static int parse_version(const char *s, unsigned v[3]) {
    for (int i = 0; i < 3; ++i) {
        if (*s < '0' || *s > '9') return 0;
        if (s[0] == '0' && s[1] >= '0' && s[1] <= '9') return 0;
        unsigned value = 0;
        int digits = 0;
        while (*s >= '0' && *s <= '9') {
            value = value * 10 + (unsigned)(*s - '0');
            if (++digits > 5) return 0;
            ++s;
        }
        v[i] = value;
        if (i < 2) {
            if (*s != '.') return 0;
            ++s;
        }
    }
    return *s == 0;
}

static int cmp_version(const unsigned a[3], const unsigned b[3]) {
    for (int i = 0; i < 3; ++i) {
        if (a[i] != b[i]) return a[i] < b[i] ? -1 : 1;
    }
    return 0;
}

static int app_complete(const wchar_t *root) {
    wchar_t path[PATH_CAP];
    return join_w(path, root, L"runtime\\python\\pythonw.exe") && is_file(path) &&
           join_w(path, root, L"ametech\\__main__.py") && is_file(path);
}

/* Choisit le dossier à exécuter : version courante installée ou version livrée. */
static int resolve_root(wchar_t *root) {
    wchar_t path[PATH_CAP], candidate[PATH_CAP], wide_version[64];
    char bundle_text[64], current_text[64];
    unsigned bundle_version[3] = {0, 0, 0}, current_version[3];
    int bundle_ok = app_complete(bundle_dir);
    int bundle_known = join_w(path, bundle_dir, L"version.txt") && read_small(path, bundle_text, sizeof(bundle_text)) &&
                       parse_version(bundle_text, bundle_version);
    if (data_dir[0] && join_w(path, data_dir, L"courante.txt") &&
        read_small(path, current_text, sizeof(current_text)) && parse_version(current_text, current_version) &&
        MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, current_text, -1, wide_version, 64) &&
        join_w(candidate, data_dir, L"versions") && append_w(candidate, PATH_CAP, L"\\") &&
        append_w(candidate, PATH_CAP, wide_version) && app_complete(candidate) &&
        (!bundle_ok || !bundle_known || cmp_version(current_version, bundle_version) >= 0)) {
        return copy_w(root, PATH_CAP, candidate);
    }
    if (bundle_ok) return copy_w(root, PATH_CAP, bundle_dir);
    return 0;
}

/* Guillemets Windows pour un argument (espaces, guillemets, barres obliques inverses). */
static int quote_arg(wchar_t *output, size_t cap, const wchar_t *value) {
    if (!append_w(output, cap, L"\"")) return 0;
    size_t slash_count = 0;
    for (const wchar_t *p = value; ; ++p) {
        if (*p == L'\\') { slash_count++; continue; }
        size_t repeat = (*p == L'"' || *p == 0) ? slash_count * 2 : slash_count;
        if (*p == L'"') repeat++;
        for (size_t i = 0; i < repeat; ++i) if (!append_w(output, cap, L"\\")) return 0;
        slash_count = 0;
        if (*p == 0) break;
        wchar_t letter[2] = {*p, 0};
        if (!append_w(output, cap, letter)) return 0;
    }
    return append_w(output, cap, L"\"");
}

static int prepare(const wchar_t *root, const wchar_t *arguments, wchar_t *python, wchar_t *command) {
    if (!join_w(python, root, L"runtime\\python\\pythonw.exe")) return 0;
    command[0] = 0;
    if (!quote_arg(command, COMMAND_CAP, python) || !append_w(command, COMMAND_CAP, L" -I -m ametech")) return 0;
    if (arguments && arguments[0] &&
        (!append_w(command, COMMAND_CAP, L" ") || !append_w(command, COMMAND_CAP, arguments))) return 0;
    SetEnvironmentVariableW(L"AMETECH_LANCEUR", exe_path);
    SetEnvironmentVariableW(L"AMETECH_RACINE", root);
    SetEnvironmentVariableW(L"AMETECH_DONNEES", data_dir[0] ? data_dir : NULL);
    return 1;
}

static void write_pid(DWORD pid) {
    wchar_t path[PATH_CAP];
    char text[32];
    DWORD written;
    if (!data_dir[0] || !join_w(path, data_dir, L"enfant.pid")) return;
    HANDLE file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return;
    int n = snprintf(text, sizeof(text), "%lu\r\n", (unsigned long)pid);
    if (n > 0) WriteFile(file, text, (DWORD)n, &written, NULL);
    CloseHandle(file);
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show) {
    (void)instance; (void)previous; (void)show;
    wchar_t root[PATH_CAP], python[PATH_CAP], command[COMMAND_CAP];
    DWORD length = GetModuleFileNameW(NULL, exe_path, PATH_CAP);
    if (!length || length >= PATH_CAP || !copy_w(bundle_dir, PATH_CAP, exe_path)) {
        message(L"Chemin du dossier trop long. Extrayez le ZIP dans un dossier plus court.");
        return 2;
    }
    wchar_t *slash = wcsrchr(bundle_dir, L'\\');
    if (!slash) return 2;
    *slash = 0;
    init_data_dir();
    init_log();
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    if (!resolve_root(root)) {
        message(L"Le dossier AME Tech est incomplet.\n\nExtrayez de nouveau tout le ZIP, puis lancez AME Tech.exe depuis le dossier extrait. Aucune installation de Python n'est requise.");
        return 2;
    }

    HANDLE mutex = CreateMutexW(NULL, FALSE, L"Local\\AMETechLanceur");
    if (!mutex) { message(L"Impossible de réserver AME Tech dans cette session Windows."); return 3; }
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        /* Instance déjà active : l'application rouvre sa fenêtre puis s'arrête. */
        STARTUPINFOW startup = {0};
        PROCESS_INFORMATION child = {0};
        startup.cb = sizeof(startup);
        if (prepare(root, arguments, python, command) &&
            CreateProcessW(python, command, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, root, &startup, &child)) {
            CloseHandle(child.hThread);
            CloseHandle(child.hProcess);
        } else {
            message(L"AME Tech est déjà ouvert. Retrouvez sa fenêtre dans la barre des tâches.");
        }
        CloseHandle(mutex);
        if (log_handle != INVALID_HANDLE_VALUE) CloseHandle(log_handle);
        return 0;
    }

    HANDLE job = CreateJobObjectW(NULL, NULL);
    if (!job) { log_event("creation du job impossible", GetLastError()); CloseHandle(mutex); return 3; }
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
        log_event("configuration du job impossible", GetLastError()); CloseHandle(job); CloseHandle(mutex); return 3;
    }

    unsigned crashes = 0, quick_restarts = 0;
    DWORD final_code = 0;
    const DWORD delays[] = {1000, 2000, 5000, 10000, 30000};
    while (1) {
        if (!resolve_root(root)) {
            message(L"Le dossier AME Tech est incomplet. Extrayez de nouveau tout le ZIP.");
            final_code = 2; break;
        }
        if (!prepare(root, arguments, python, command)) {
            message(L"La commande de lancement est trop longue."); final_code = 2; break;
        }
        STARTUPINFOW startup = {0};
        startup.cb = sizeof(startup);
        if (log_handle != INVALID_HANDLE_VALUE) {
            startup.dwFlags = STARTF_USESTDHANDLES;
            startup.hStdInput = NULL;
            startup.hStdOutput = log_handle;
            startup.hStdError = log_handle;
        }
        PROCESS_INFORMATION child = {0};
        ULONGLONG started = GetTickCount64();
        if (!CreateProcessW(python, command, NULL, NULL, log_handle != INVALID_HANDLE_VALUE,
                            CREATE_NO_WINDOW | CREATE_SUSPENDED, NULL, root, &startup, &child)) {
            log_event("lancement de Python impossible", GetLastError());
            message(L"Windows n'a pas pu ouvrir le moteur inclus.\n\nVérifiez que le ZIP est entièrement extrait et que Windows est en 64 bits. Le journal se trouve dans %LOCALAPPDATA%\\AME Tech\\lanceur.log.");
            final_code = 3; break;
        }
        if (!AssignProcessToJobObject(job, child.hProcess)) {
            log_event("rattachement au job impossible", GetLastError());
            TerminateProcess(child.hProcess, 3);
            CloseHandle(child.hThread); CloseHandle(child.hProcess);
            message(L"Windows empêche la protection du processus. Fermez puis relancez AME Tech depuis l'Explorateur.");
            final_code = 3; break;
        }
        if (ResumeThread(child.hThread) == (DWORD)-1) {
            log_event("demarrage du processus impossible", GetLastError());
            TerminateProcess(child.hProcess, 3);
            CloseHandle(child.hThread); CloseHandle(child.hProcess);
            message(L"Windows n'a pas pu démarrer AME Tech. Fermez-le puis relancez-le.");
            final_code = 3; break;
        }
        CloseHandle(child.hThread);
        write_pid(child.dwProcessId);
        log_event("application demarree", child.dwProcessId);
        if (WaitForSingleObject(child.hProcess, INFINITE) != WAIT_OBJECT_0 ||
            !GetExitCodeProcess(child.hProcess, &final_code)) {
            log_event("attente du processus impossible", GetLastError());
            TerminateProcess(child.hProcess, 3);
            final_code = 3;
        }
        CloseHandle(child.hProcess);
        log_event("application arretee", final_code);
        if (final_code == 0) break;
        ULONGLONG lifetime = GetTickCount64() - started;
        if (final_code == CODE_RELANCE) {
            quick_restarts = lifetime >= RELANCE_RAPIDE_MS ? 1 : quick_restarts + 1;
            if (quick_restarts > MAX_RELANCES_RAPIDES) {
                message(L"AME Tech a demandé trop de relances rapprochées. Relancez-le à la main ; le journal se trouve dans %LOCALAPPDATA%\\AME Tech\\lanceur.log.");
                break;
            }
            log_event("relance demandee", quick_restarts);
            continue;
        }
        if (lifetime >= STABLE_MS) crashes = 0;
        crashes++;
        if (crashes > MAX_CRASHES) {
            message(L"AME Tech s'est arrêté plusieurs fois malgré les relances automatiques.\n\nLe journal se trouve dans %LOCALAPPDATA%\\AME Tech\\lanceur.log. Relancez AME Tech ; aucun redémarrage en boucle ne sera effectué.");
            break;
        }
        log_event("relance automatique dans (ms)", delays[crashes - 1]);
        Sleep(delays[crashes - 1]);
    }
    CloseHandle(job);
    CloseHandle(mutex);
    if (log_handle != INVALID_HANDLE_VALUE) CloseHandle(log_handle);
    return (int)final_code;
}
