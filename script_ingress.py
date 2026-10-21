import os
import sys
import re
from io import StringIO

from github import Github, GithubException, Auth
from ruamel.yaml import YAML



REPOSITORIES = [
    {"name": "pagopa/pagopa-api-config"},
]

CHART_VERSION = "8.8.2"
TASK = "PIDM-2502"

PR_BODY_HEADER = """<!--- Please always add a PR description as if nobody knows anything about the context these changes come from. -->
<!--- Even if we are all from our internal team, we may not be on the same page. -->
<!--- Write this PR as you were contributing to a public OSS project, where nobody knows you and you have to earn their trust. -->
<!--- This will improve our projects in the long run! Thanks. -->

#### List of Changes

<!--- Describe your changes in detail -->
"""

PR_BODY_FOOTER = f"""
#### Motivation and Context

<!--- Why is this change required? What problem does it solve? -->
[{TASK}](https://pagopa.atlassian.net/browse/{TASK})

Nginx will be EOL so its needed to migrate to HAProxy.

#### How Has This Been Tested?

<!--- Please describe in detail how you tested your changes. -->
<!--- Include details of your testing environment, tests ran to see how -->
<!--- your change affects other areas of the code, etc. -->

#### Screenshots (if appropriate):

#### Types of changes

<!--- What types of changes does your code introduce? Put an `x` in all the boxes that apply: -->

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [X] Breaking change (fix or feature that would cause existing functionality to not work as expected)

#### Checklist:

<!--- Go over all the following points, and put an `x` in all the boxes that apply. -->
<!--- If you're unsure about any of these, don't hesitate to ask. We're here to help! -->

- [ ] My change requires a change to the documentation.
- [ ] I have updated the documentation accordingly.
"""


# ============================================================
# PR 1 - CHART + KEDA
# ============================================================

BRANCH_NAME_1 = f"{TASK}-update-microservice-chart-8-8-2"

COMMIT_MESSAGE_1 = (
    f"chore: update microservice-chart to {CHART_VERSION}"
)

PR_TITLE_1 = (
    f"[{TASK}] chore: Update microservice-chart to {CHART_VERSION}"
)

PR_BODY_1 = (
    PR_BODY_HEADER
    + f"- Update microservice-chart to {CHART_VERSION}\n"
    + "- Move KEDA metadata.type to trigger metricType\n"
    + PR_BODY_FOOTER
)


# ============================================================
# PR 2 - HAPROXY
# ============================================================

BRANCH_NAME_2 = f"{TASK}-update-ingress-haproxy"

COMMIT_MESSAGE_2 = "chore: configure haproxy block"

PR_TITLE_2 = (
    f"[{TASK}] chore: Configure HAProxy ingress"
)

PR_BODY_2 = (
    PR_BODY_HEADER
    + "- Migrate ingress from nginx to HAProxy (add haproxy block)\n"
    + PR_BODY_FOOTER
)


# ============================================================
# YAML
# ============================================================

def get_yaml_parser():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


# ============================================================
# CHART.YAML
# ============================================================

def process_chart_yaml(content_str):
    """
    Aggiorna tutte le dependency microservice-chart
    alla versione CHART_VERSION.
    """

    yaml_parser = get_yaml_parser()

    try:
        data = yaml_parser.load(content_str)
    except Exception as e:
        print(f"WARN: impossibile parsare Chart.yaml: {e}")
        return None, False

    if not isinstance(data, dict):
        return None, False

    dependencies = data.get("dependencies")

    if not isinstance(dependencies, list):
        return None, False

    modified = False

    for dep in dependencies:

        if not isinstance(dep, dict):
            continue

        if dep.get("name") != "microservice-chart":
            continue

        if str(dep.get("version")) != CHART_VERSION:

            old_version = dep.get("version")

            print(
                f"CHART: microservice-chart "
                f"{old_version} -> {CHART_VERSION}"
            )

            dep["version"] = CHART_VERSION
            modified = True

    if not modified:
        return None, False

    stream = StringIO()
    yaml_parser.dump(data, stream)

    return stream.getvalue(), True


# ============================================================
# KEDA
# ============================================================

def process_keda_values_yaml(content_str):
    """
    Migrazione KEDA eseguita testualmente.

    Trasforma, all'interno di un trigger:

        - type: azure-servicebus
          metadata:
            type: Utilization
            queueName: ...

    in:

        - type: azure-servicebus
          metricType: Utilization
          metadata:
            queueName: ...

    Non viene parsato l'intero YAML per evitare problemi
    con duplicate keys, anchor e !!merge.
    """

    lines = content_str.splitlines(keepends=True)

    modifications = []

    # Cerchiamo le righe "metadata:"
    for metadata_index, line in enumerate(lines):

        match = re.match(
            r"^(\s*)metadata:\s*(?:#.*)?(?:\r?\n)?$",
            line,
        )

        if not match:
            continue

        metadata_indent = len(match.group(1))

        # ----------------------------------------------------
        # Verifica che metadata appartenga ad un trigger.
        #
        # Cerchiamo all'indietro una riga "- type:"
        # con indentazione inferiore a metadata.
        # ----------------------------------------------------

        trigger_found = False
        trigger_indent = None

        for j in range(metadata_index - 1, -1, -1):

            previous = lines[j]

            if not previous.strip():
                continue

            if previous.lstrip().startswith("#"):
                continue

            previous_indent = (
                len(previous)
                - len(previous.lstrip(" "))
            )

            if previous_indent >= metadata_indent:
                continue

            trigger_match = re.match(
                r"^(\s*)-\s+type:\s*(.+?)\s*(?:#.*)?(?:\r?\n)?$",
                previous,
            )

            if trigger_match:
                trigger_found = True
                trigger_indent = len(trigger_match.group(1))
                break

            # Siamo usciti dal possibile trigger
            if previous_indent < metadata_indent - 2:
                break

        if not trigger_found:
            continue

        # ----------------------------------------------------
        # Cerca "type:" dentro metadata
        # ----------------------------------------------------

        metadata_type_index = None
        metadata_type_value = None

        for j in range(metadata_index + 1, len(lines)):

            current = lines[j]

            if not current.strip():
                continue

            if current.lstrip().startswith("#"):
                continue

            current_indent = (
                len(current)
                - len(current.lstrip(" "))
            )

            # Fine metadata
            if current_indent <= metadata_indent:
                break

            type_match = re.match(
                r"^\s*type:\s*(.+?)\s*(?:#.*)?(?:\r?\n)?$",
                current,
            )

            if type_match:
                metadata_type_index = j
                metadata_type_value = (
                    type_match.group(1).strip()
                )
                break

        if metadata_type_index is None:
            continue

        # ----------------------------------------------------
        # Controlla se metricType è già presente nel trigger
        # ----------------------------------------------------

        metric_type_exists = False

        for j in range(metadata_index - 1, -1, -1):

            current = lines[j]

            if not current.strip():
                continue

            current_indent = (
                len(current)
                - len(current.lstrip(" "))
            )

            if current_indent < trigger_indent:
                break

            if re.match(
                r"^\s*metricType:\s*",
                current,
            ):
                metric_type_exists = True
                break

        if metric_type_exists:

            print(
                f"SKIP KEDA: metadata riga "
                f"{metadata_index + 1}: "
                f"metricType già presente"
            )

            continue

        # metricType deve stare allo stesso livello di metadata
        metric_indent = " " * metadata_indent

        newline = (
            "\r\n"
            if line.endswith("\r\n")
            else "\n"
        )

        metric_line = (
            f"{metric_indent}"
            f"metricType: {metadata_type_value}"
            f"{newline}"
        )

        modifications.append(
            {
                "metadata_index": metadata_index,
                "type_index": metadata_type_index,
                "metric_line": metric_line,
                "value": metadata_type_value,
            }
        )

    if not modifications:
        return None, False

    # --------------------------------------------------------
    # Applica dal basso verso l'alto
    # --------------------------------------------------------

    for modification in sorted(
        modifications,
        key=lambda x: x["metadata_index"],
        reverse=True,
    ):

        metadata_index = modification["metadata_index"]
        type_index = modification["type_index"]
        metric_line = modification["metric_line"]
        value = modification["value"]

        print(
            f"KEDA: metadata.type '{value}' "
            f"-> metricType"
        )

        # Prima rimuoviamo type da metadata.
        del lines[type_index]

        # Se type era dopo metadata, la posizione metadata
        # non cambia.
        lines.insert(metadata_index, metric_line)

    return "".join(lines), True


# ============================================================
# CHART JOB
# ============================================================

def process_chart_job(content_str, filename):

    filename_lower = filename.lower()

    if filename_lower == "chart.yaml":
        return process_chart_yaml(content_str)

    if (
        filename_lower.startswith("values")
        and filename_lower.endswith((".yaml", ".yml"))
    ):
        return process_keda_values_yaml(content_str)

    return None, False


# ============================================================
# HAPROXY PATH
# ============================================================

def extract_haproxy_path(full_path):

    if not full_path:
        return "/", "/"

    full_path = str(full_path).strip()

    # Esempio:
    #
    # /pagopa-api-config-core-service/p/(.*)
    #
    # diventa:
    #
    # /pagopa-api-config-core-service/p/

    if full_path.endswith("(.*)"):

        haproxy_path = full_path[:-4]

        if not haproxy_path.endswith("/"):
            haproxy_path += "/"

        return haproxy_path, "/"

    return full_path, "/"


# ============================================================
# HAPROXY
# ============================================================

def process_values_yaml(content_str, filename=None):
    """
    Aggiunge HAProxy solamente agli ingress che dichiarano
    esplicitamente un path.

    Non modifica:

      - canaryDelivery.ingress senza path
      - ingress base usato come anchor (&ingress)
      - ingress senza path
      - ingress che hanno già haproxy
    """

    lines = content_str.splitlines(keepends=True)

    blocks = []

    # --------------------------------------------------------
    # 1. Individua tutti i blocchi ingress
    # --------------------------------------------------------

    for i, line in enumerate(lines):

        match = re.match(
            r"^(\s*)ingress:\s*"
            r"(?:&[A-Za-z0-9_-]+)?\s*"
            r"(?:#.*)?"
            r"(?:\r?\n)?$",
            line,
        )

        if not match:
            continue

        indent = len(match.group(1))

        end = len(lines)

        for j in range(i + 1, len(lines)):

            current = lines[j]

            if not current.strip():
                continue

            if current.lstrip().startswith("#"):
                continue

            current_indent = (
                len(current)
                - len(current.lstrip(" "))
            )

            if current_indent <= indent:
                end = j
                break

        blocks.append(
            {
                "start": i,
                "end": end,
                "indent": indent,
            }
        )

    if not blocks:
        print(
            f"WARN: nessun ingress trovato"
            f"{f' in {filename}' if filename else ''}"
        )
        return None, False

    # --------------------------------------------------------
    # 2. Recupera host comune dall'anchor &ingress
    # --------------------------------------------------------

    common_host = None

    for block in blocks:

        ingress_line = lines[block["start"]]

        if "&ingress" not in ingress_line:
            continue

        body = "".join(
            lines[
                block["start"] + 1:
                block["end"]
            ]
        )

        host_match = re.search(
            r'(?m)^\s+host:\s*["\']?'
            r'([^"\'\r\n#]+)'
            r'["\']?\s*(?:#.*)?$',
            body,
        )

        if host_match:

            common_host = (
                host_match.group(1).strip()
            )

            break

    # --------------------------------------------------------
    # 3. Aggiunge HAProxy agli ingress con path
    # --------------------------------------------------------

    insertions = []

    for block in blocks:

        body = "".join(
            lines[
                block["start"] + 1:
                block["end"]
            ]
        )

        # HAProxy già presente
        if re.search(
            r"(?m)^\s+haproxy:\s*(?:#.*)?$",
            body,
        ):

            print(
                f"SKIP: ingress riga "
                f"{block['start'] + 1}: "
                f"HAProxy già presente"
            )

            continue

        # Deve esserci un path esplicito
        path_match = re.search(
            r'(?m)^\s+path:\s*["\']?'
            r'([^"\'\r\n#]+)'
            r'["\']?\s*(?:#.*)?$',
            body,
        )

        if not path_match:

            print(
                f"SKIP: ingress riga "
                f"{block['start'] + 1}: "
                f"nessun path esplicito"
            )

            continue

        path = path_match.group(1).strip()

        # ----------------------------------------------------
        # Host
        # ----------------------------------------------------

        host_match = re.search(
            r'(?m)^\s+host:\s*["\']?'
            r'([^"\'\r\n#]+)'
            r'["\']?\s*(?:#.*)?$',
            body,
        )

        if host_match:

            host = (
                host_match.group(1).strip()
            )

        else:

            # Esempio:
            #
            # ingress:
            #   !!merge <<: *ingress
            #   path: ...
            #
            # L'host viene ereditato dall'anchor.

            host = common_host

        if not host:

            print(
                f"WARN: ingress riga "
                f"{block['start'] + 1}: "
                f"host non trovato"
            )

            continue

        # ----------------------------------------------------
        # Path
        # ----------------------------------------------------

        haproxy_path, rewrite_target = (
            extract_haproxy_path(path)
        )

        child_indent = (
            " " * (block["indent"] + 2)
        )

        grandchild_indent = (
            " " * (block["indent"] + 4)
        )

        block_text = (
            f"{child_indent}haproxy:\n"
            f"{grandchild_indent}create: true\n"
            f'{grandchild_indent}host: "{host}"\n'
            f'{grandchild_indent}path: "{haproxy_path}"\n'
            f'{grandchild_indent}'
            f'rewriteTarget: "{rewrite_target}"\n'
            f"{grandchild_indent}annotations:\n"
            f'{grandchild_indent}  '
            f'haproxy.org/check: "true"\n'
        )

        insertions.append(
            (block["end"], block_text)
        )

    if not insertions:
        return None, False

    # Inserimento dal basso verso l'alto
    for index, text in sorted(
        insertions,
        key=lambda x: x[0],
        reverse=True,
    ):
        lines.insert(index, text)

    return "".join(lines), True


# ============================================================
# JOBS
# ============================================================

JOBS = [
    {
        "name": "chart",
        "branch": BRANCH_NAME_1,
        "commit_message": COMMIT_MESSAGE_1,
        "pr_title": PR_TITLE_1,
        "pr_body": PR_BODY_1,

        # PR 1 deve controllare sia Chart.yaml
        # sia i values per la migrazione KEDA.
        "match_file": lambda name: (
            name.lower() == "chart.yaml"
            or (
                name.lower().startswith("values")
                and name.lower().endswith(
                    (".yaml", ".yml")
                )
            )
        ),

        "processor": process_chart_job,
    },

    {
        "name": "haproxy",
        "branch": BRANCH_NAME_2,
        "commit_message": COMMIT_MESSAGE_2,
        "pr_title": PR_TITLE_2,
        "pr_body": PR_BODY_2,

        "match_file": lambda name: (
            name.lower().startswith("values")
            and name.lower().endswith(
                (".yaml", ".yml")
            )
        ),

        "processor": process_values_yaml,
    },
]


# ============================================================
# REPOSITORY PROCESSING
# ============================================================

def process_repository(
    gh,
    repo_name,
    job,
    pr_elaborated,
):

    branch_name = job["branch"]

    print(
        f"\n---> [{job['name']}] "
        f"Modifying Repo: {repo_name}"
    )

    # --------------------------------------------------------
    # Repository
    # --------------------------------------------------------

    try:
        repo = gh.get_repo(repo_name)

    except GithubException as e:

        print(
            f"ERROR: Impossible to access repo "
            f"{repo_name}: {e}"
        )

        return

    default_branch = repo.default_branch

    # --------------------------------------------------------
    # Branch
    # --------------------------------------------------------

    branch_exists = False

    try:

        repo.get_git_ref(
            f"heads/{branch_name}"
        )

        branch_exists = True

        print(
            f"The branch '{branch_name}' "
            f"already exists. "
            f"It will be checked and possibly updated."
        )

    except GithubException as e:

        # Creiamo la branch solamente se realmente non esiste.
        if e.status != 404:

            print(
                f"ERROR checking branch "
                f"'{branch_name}': {e}"
            )

            return

        try:

            source_ref = repo.get_git_ref(
                f"heads/{default_branch}"
            )

            repo.create_git_ref(
                ref=f"refs/heads/{branch_name}",
                sha=source_ref.object.sha,
            )

            print(
                f"Branch '{branch_name}' "
                f"created successfully."
            )

        except GithubException as create_error:

            print(
                f"ERROR creating branch "
                f"'{branch_name}': "
                f"{create_error}"
            )

            return

    # --------------------------------------------------------
    # IMPORTANTE:
    #
    # La branch, anche appena creata, è il riferimento
    # su cui dobbiamo leggere e scrivere.
    # --------------------------------------------------------

    target_ref = branch_name

    # --------------------------------------------------------
    # Helm files
    # --------------------------------------------------------

    try:

        helm_contents = repo.get_contents(
            "helm",
            ref=target_ref,
        )

    except GithubException:

        print(
            f"WARN: Folder 'helm/' not found "
            f"in {repo_name}. Skipping."
        )

        return

    files_to_process = []

    while helm_contents:

        file_content = helm_contents.pop(0)

        if file_content.type == "dir":

            helm_contents.extend(
                repo.get_contents(
                    file_content.path,
                    ref=target_ref,
                )
            )

        elif (
            file_content.name.lower().endswith(
                (".yaml", ".yml")
            )
            and job["match_file"](
                file_content.name
            )
        ):

            files_to_process.append(
                file_content
            )

    if not files_to_process:

        print(
            f"No YAML files found for "
            f"job '{job['name']}'."
        )

        return

    updated_any = False

    # --------------------------------------------------------
    # Process files
    # --------------------------------------------------------

    for file_info in files_to_process:

        try:

            # Recuperiamo sempre la versione più recente
            # dalla branch target.
            target_file_info = repo.get_contents(
                file_info.path,
                ref=branch_name,
            )

        except GithubException as e:

            print(
                f"WARN: impossibile leggere "
                f"{file_info.path} "
                f"dalla branch {branch_name}: {e}"
            )

            continue

        decoded_content = (
            target_file_info
            .decoded_content
            .decode("utf-8")
        )

        new_content, is_modified = (
            job["processor"](
                decoded_content,
                target_file_info.name,
            )
        )

        if not is_modified:

            print(
                f"File already updated / "
                f"no changes for: "
                f"{target_file_info.path}"
            )

            continue

        print(
            f"Update/Modification applied to: "
            f"{target_file_info.path}"
        )

        try:

            repo.update_file(
                path=target_file_info.path,
                message=(
                    f"{job['commit_message']} "
                    f"on {target_file_info.name}"
                ),
                content=new_content,
                sha=target_file_info.sha,
                branch=branch_name,
            )

            updated_any = True

        except GithubException as e:

            print(
                f"ERROR updating "
                f"{target_file_info.path}: {e}"
            )

    # --------------------------------------------------------
    # Pull Request
    # --------------------------------------------------------

    try:

        prs = repo.get_pulls(
            state="open",
            head=(
                f"{repo.owner.login}:"
                f"{branch_name}"
            ),
        )

        # PR già presente
        if prs.totalCount > 0:

            pr = prs[0]

            if updated_any:

                print(
                    f"SUCCESS: PR #{pr.number} "
                    f"updated with new commits -> "
                    f"{pr.html_url}"
                )

            else:

                print(
                    f"Info: PR #{pr.number} "
                    f"is already open and "
                    f"up-to-date -> "
                    f"{pr.html_url}"
                )

            pr_elaborated["total"] += 1

            if pr.html_url not in pr_elaborated["pr_links"]:
                pr_elaborated["pr_links"].append(
                    pr.html_url
                )

            return

        # ----------------------------------------------------
        # Nessuna PR aperta.
        #
        # Controlliamo se la branch ha veramente commit
        # diversi da main.
        # ----------------------------------------------------

        comparison = repo.compare(
            default_branch,
            branch_name,
        )

        if comparison.ahead_by == 0:

            print(
                f"Info: job '{job['name']}': "
                f"nessuna differenza tra "
                f"'{branch_name}' e "
                f"'{default_branch}'. "
                f"PR non necessaria."
            )

            return

        # ----------------------------------------------------
        # Crea PR
        # ----------------------------------------------------

        pr = repo.create_pull(
            title=job["pr_title"],
            body=job["pr_body"],
            head=branch_name,
            base=default_branch,
        )

        print(
            f"SUCCESS: New Pull Request "
            f"created -> {pr.html_url}"
        )

        pr_elaborated["total"] += 1
        pr_elaborated["pr_links"].append(
            pr.html_url
        )

    except GithubException as e:

        print(
            f"Info/Error managing PR: {e}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    token = os.getenv("GITHUB_TOKEN")

    if not token:

        print(
            "ERROR: Env GITHUB_TOKEN "
            "is not configured."
        )

        sys.exit(1)

    # PyGithub moderno:
    # evita il DeprecationWarning di Github(token)
    auth = Auth.Token(token)
    gh = Github(auth=auth)

    pr_elaborated = {
        "total": 0,
        "pr_links": [],
    }

    for repo_info in REPOSITORIES:

        for job in JOBS:

            process_repository(
                gh,
                repo_info["name"],
                job,
                pr_elaborated,
            )

    print("\n==============================")
    print("RESULT")
    print("==============================")

    print(
        f"Total PR: "
        f"{pr_elaborated['total']}"
    )

    for pr_link in pr_elaborated["pr_links"]:
        print(f"- {pr_link}")


if __name__ == "__main__":
    main()