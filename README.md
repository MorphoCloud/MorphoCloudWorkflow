# MorphoCloudWorkflow

A collection of reusable GitHub workflows for managing on-demand JetStream2
virtual machines.

This repository provides GitHub workflows and composite actions designed for
easy integration into target GitHub projects via vendoring. These workflows
enable researchers to easily request and manage cloud instances by simply
creating issues within their GitHub projects.

Once approved, cloud instance requests are provisioned within the
[ACCESS allocation](https://allocations.access-ci.org/) associated with the
GitHub project.

Tasks such as instance creation, deletion, shelving, and unshelving are
initiated by adding issue comments that follow the `/action` pattern. For a full
list of supported commands, see the [documentation](issue-commands.md)

## Requirements

To ensure successful provisioning of requested instances, follow these steps to
configure the necessary infrastructure:

1. **Request an ACCESS Allocation**

   Visit the [ACCESS allocations portal](https://allocations.access-ci.org/) to
   request a new allocation for your project.

2. **Set Up a GitHub Organization**

   Identify or create a GitHub organization to host your project and integrate
   the workflows.

3. **Create a GitHub Project**

   The GitHub issue tracker within the project allows researchers to request
   instances within the ACCESS allocation. Instance requests are automatically
   formatted according to the predefined structure in the
   [request.yml](.github/ISSUE_TEMPLATE/request.yml) issue template.

4. **Register a New GitHub Application**

   You'll need a GitHub App to enable workflows to authenticate via the GitHub
   API and support triggering other workflows. Follow these steps to register
   and configure the app:

   1. Register a
      [new GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app):

      - Set the following options:

        - GitHub App name (e.g `YourProject Workflow App`)
        - Homepage URL (e.g `https://github.com/YourOrganization/YourProject`)
        - Permissions:
          - Repository Permissions:
            - Actions: Read & Write
            - Contents: Read & Write
            - Issues: Read & Write
            - Metadata: Read-only
          - Organization Permissions:
            - Members: Read-only
        - Disable the webhook.
        - The default "Only on this account" installation restriction works
          well.

      - Register the app and note down the **App ID**, which will be required
        later to set the `MORPHOCLOUD_WORKFLOW_APP_ID` repository variable.

   2. Generate a new GitHub App client secret.

   3. Generate a GitHub App private key. The content of the `.pem` file will be
      used to set the `MORPHOCLOUD_WORKFLOW_APP_PRIVATE_KEY` repository secret.

5. **Create a Dedicated Gmail Account**

   Consider creating a dedicated Gmail account to handle email notifications.
   Follow the setup instructions in the
   [action-send-mail documentation](https://github.com/dawidd6/action-send-mail#gmail).
   This will provide credentials for the `MAIL_USERNAME` and `MAIL_PASSWORD`
   repository secrets.

6. **Generate an Encryption Key**

   Use a password generator to create a secure encryption key. The value will be
   used below to set the `STRING_ENCRYPTION_KEY` repository secret.

   The encryption key is used to securely encode and decode the researcher’s
   email address when requesting an instance.

7. **Set Up and Register a MorphoCloud GitHub Runner**

   See the [instructions below](#setting-up-a-morphocloud-github-runner) to set
   up the runner.

8. **Configure Repository Secrets and Variables**

   In your GitHub project, set up the following repository secrets and
   variables:

   | Name                                   | Repository Variable | Repository Secret  |
   | -------------------------------------- | ------------------- | ------------------ |
   | `MORPHOCLOUD_OS_CLOUD`                 | :white_check_mark:  |                    |
   | `MORPHOCLOUD_GITHUB_ADMINS`            | :white_check_mark:  |                    |
   | `MORPHOCLOUD_WORKFLOW_APP_ID`          | :white_check_mark:  |                    |
   | `MORPHOCLOUD_WORKFLOW_APP_PRIVATE_KEY` |                     | :white_check_mark: |
   | `STRING_ENCRYPTION_KEY`                |                     | :white_check_mark: |
   | `MAIL_USERNAME`                        |                     | :white_check_mark: |
   | `MAIL_PASSWORD`                        |                     | :white_check_mark: |

   - `MORPHOCLOUD_GITHUB_ADMINS`: This variable should be set as a
     comma-separated list of GitHub handles (e.g., "jcfr,muratmaga").

9. **Vendorize Workflow into Your GitHub Project**

   To vendorize the workflow into your project, run the following commands:

   ```bash
   git clone git@github.com:YourOrganization/YourProject

   pipx run nox -s vendorize -- --commit /path/to/YourProject

   cd /path/to/YourProject

   git push origin main
   ```

## Setting Up a MorphoCloud GitHub Runner

1. Log in to [JetStream2 Exosphere](https://jetstream2.exosphere.app/exosphere/)
   and select the appropriate allocation.

2. Create a `m3.tiny` instance with a **custom root disk size** of 30GB.

3. Upload your SSH key and associate it with the instance.

4. Disable the web desktop feature for the instance.

5. In _Advanced Options_, disable the deployment of Guacamole.

6. Once the instance is created, connect to it via SSH.

7. Set up a Python virtual environment and install the OpenStack client:

   ```bash
   python3 -m venv ~/venv
   ~/venv/bin/python -m pip install python-openstackclient
   ```

8. Add or update the OpenStack configuration file
   (`~/.config/openstack/clouds.yaml`). You can retrieve the file from the
   _clouds.yaml_ link in the "Credentials" section of your allocation in the
   Exosphere UI.

   The OpenStack cloud allocation name (e.g., `BIO180006_IU`) will be used to
   set the `MORPHOCLOUD_OS_CLOUD` repository variable.

9. Install `jq`:

   ```bash
   sudo apt-get install -y jq
   ```

10. Install and register the GitHub runner:

    Follow these steps to install and register a GitHub runner:

    1. In your GitHub repository, go to _Settings_ -> _Actions_ -> _New
       self-hosted runner_.

    2. Choose _Linux_ and _x86_ architecture.

    3. After connecting to the runner instance via SSH, create a directory for
       your project:

       ```bash
       project_name=<YourProject>
       mkdir ~/$project_name && cd ~/$project_name
       ```

    4. Follow the instructions in the GitHub UI to download and extract the
       runner package:

       ```bash
       mkdir actions-runner && cd actions-runner
       # Use the "curl" command to download the runner package
       # Verify the package using "shasum"
       # Extract the package using "tar"
       ```

    5. Follow the GitHub UI instructions to configure the runner using the
       `./config.sh` script. Specify the `url` and `token` provided by GitHub:

       ```bash
       ./config.sh --url https://github.com/YourOrganization/YourProject --token TOKEN
       ```

    6. Start the runner and ensure it's connected to GitHub:

       ```bash
       ./run.sh

       √ Connected to GitHub

       Current runner version: 'X.Y.Z'
       YYYY-MM-DD 00:00:00Z: Listening for Jobs
       ```

    7. Stop the runner and configure it to run as a service by following the
       [GitHub documentation](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/configuring-the-self-hosted-runner-application-as-a-service):

       ```bash
       sudo ./svc.sh install
       sudo ./svc.sh start
       sudo ./svc.sh status
       ```
