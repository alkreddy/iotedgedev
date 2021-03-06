import os
import re
import shutil
import sys
from zipfile import ZipFile

import commentjson
from six import BytesIO
from six.moves.urllib.request import urlopen

from . import telemetry
from .buildoptionsparser import BuildOptionsParser
from .buildprofile import BuildProfile
from .compat import PY2
from .deploymentmanifest import DeploymentManifest
from .dockercls import Docker
from .dotnet import DotNet
from .module import Module
from .utility import Utility

if PY2:
    from .compat import FileNotFoundError


class Modules:
    def __init__(self, envvars, output):
        self.envvars = envvars
        self.output = output
        self.utility = Utility(self.envvars, self.output)

    def add(self, name, template, group_id):
        self.output.header("ADDING MODULE {0}".format(name))

        deployment_manifest = DeploymentManifest(self.envvars, self.output, self.utility, self.envvars.DEPLOYMENT_CONFIG_TEMPLATE_FILE, True)

        cwd = self.envvars.MODULES_PATH
        self.utility.ensure_dir(cwd)

        if name.startswith("_") or name.endswith("_"):
            raise ValueError("Module name cannot start or end with the symbol _")
        elif not re.match("^[a-zA-Z0-9_]+$", name):
            raise ValueError("Module name can only contain alphanumeric characters and the symbol _")
        elif os.path.exists(os.path.join(cwd, name)):
            raise ValueError("Module \"{0}\" already exists under {1}".format(name, os.path.abspath(self.envvars.MODULES_PATH)))

        telemetry.add_extra_props({"template": template})

        repo = "{0}/{1}".format("${CONTAINER_REGISTRY_SERVER}", name.lower())
        if template == "c":
            github_prefix = "https://github.com/Azure"
            git_repo = "azure-iot-edge-c-module"
            branch = "master"
            url = "{0}/{1}/archive/{2}.zip".format(github_prefix, git_repo, branch)
            response = urlopen(url)

            temp_dir = os.path.join(os.path.expanduser("~"), '.iotedgedev')
            self.utility.ensure_dir(temp_dir)
            zip_file_prefix = "{0}-{1}".format(git_repo, branch)
            temp_template_dir = os.path.join(temp_dir, zip_file_prefix)
            if os.path.exists(temp_template_dir):
                shutil.rmtree(temp_template_dir)

            with ZipFile(BytesIO(response.read())) as zip_f:
                zip_f.extractall(temp_dir)

            os.rename(temp_template_dir, os.path.join(cwd, name))

            module = Module(self.envvars, self.utility, name)
            module.repository = repo
            module.dump()
        elif template == "csharp":
            dotnet = DotNet(self.output, self.utility)
            dotnet.install_module_template()
            dotnet.create_custom_module(name, repo, cwd)
        elif template == "java":
            self.utility.check_dependency("mvn --help".split(), "To add new Java modules, the Maven tool")
            cmd = ["mvn",
                   "archetype:generate",
                   "-DarchetypeGroupId=com.microsoft.azure",
                   "-DarchetypeArtifactId=azure-iot-edge-archetype",
                   "-DgroupId={0}".format(group_id),
                   "-DartifactId={0}".format(name),
                   "-Dversion=1.0.0-SNAPSHOT",
                   "-Dpackage={0}".format(group_id),
                   "-Drepository={0}".format(repo),
                   "-B"]
            self.output.header(" ".join(cmd))
            self.utility.exe_proc(cmd, cwd=cwd)
        elif template == "nodejs":
            self.utility.check_dependency("yo azure-iot-edge-module --help".split(), "To add new Node.js modules, the Yeoman tool and Azure IoT Edge Node.js module generator",
                                          shell=not self.envvars.is_posix())
            cmd = "yo azure-iot-edge-module -n {0} -r {1}".format(name, repo)
            self.output.header(cmd)
            self.utility.exe_proc(cmd.split(), shell=not self.envvars.is_posix(), cwd=cwd)
        elif template == "python":
            self.utility.check_dependency("cookiecutter --help".split(), "To add new Python modules, the Cookiecutter tool")
            github_source = "https://github.com/Azure/cookiecutter-azure-iot-edge-module"
            branch = "master"
            cmd = "cookiecutter --no-input {0} module_name={1} image_repository={2} --checkout {3}".format(github_source, name, repo, branch)
            self.output.header(cmd)
            self.utility.exe_proc(cmd.split(), cwd=cwd)
        elif template == "csharpfunction":
            dotnet = DotNet(self.output, self.utility)
            dotnet.install_function_template()
            dotnet.create_function_module(name, repo, cwd)

        deployment_manifest.add_module_template(name)
        deployment_manifest.dump()

        self._update_launch_json(name, template, group_id)

        self.output.footer("ADD COMPLETE")

    def build(self):
        self.build_push(no_push=True)

    def push(self, no_build=False):
        self.build_push(no_build=no_build)

    def build_push(self, no_build=False, no_push=False):
        self.output.header("BUILDING MODULES", suppress=no_build)

        bypass_modules = self.utility.get_bypass_modules()
        active_platform = self.utility.get_active_docker_platform()

        # map (module name, platform) tuple to tag.
        # sample: (('filtermodule', 'amd64'), 'localhost:5000/filtermodule:0.0.1-amd64')
        image_tag_map = {}
        # map image tag to BuildProfile object
        tag_build_profile_map = {}
        # image tags to build
        # sample: 'localhost:5000/filtermodule:0.0.1-amd64'
        tags_to_build = set()

        for module_name in os.listdir(self.envvars.MODULES_PATH):
            try:
                module = Module(self.envvars, self.utility, module_name)
                for platform in module.platforms:
                    # get the Dockerfile from module.json
                    dockerfile = module.get_dockerfile_by_platform(platform)
                    container_tag = "" if self.envvars.CONTAINER_TAG == "" else "-" + self.envvars.CONTAINER_TAG
                    tag = "{0}:{1}{2}-{3}".format(module.repository, module.tag_version, container_tag, platform).lower()
                    image_tag_map[(module_name, platform)] = tag
                    tag_build_profile_map[tag] = BuildProfile(module_name, dockerfile, module.context_path, module.build_options)
                    if not self.utility.in_asterisk_list(module_name, bypass_modules) and self.utility.in_asterisk_list(platform, active_platform):
                        tags_to_build.add(tag)
            except FileNotFoundError:
                pass

        deployment_manifest = DeploymentManifest(self.envvars, self.output, self.utility, self.envvars.DEPLOYMENT_CONFIG_TEMPLATE_FILE, True)
        modules_to_process = deployment_manifest.get_modules_to_process()

        replacements = {}
        for module_name, platform in modules_to_process:
            key = (module_name, platform)
            if key in image_tag_map:
                tag = image_tag_map.get(key)
                replacements["${{MODULES.{0}.{1}}}".format(module_name, platform)] = tag
                if not self.utility.in_asterisk_list(module_name, bypass_modules):
                    tags_to_build.add(tag)

        for tag in tags_to_build:
            if tag in tag_build_profile_map:
                docker = Docker(self.envvars, self.utility, self.output)
                # BUILD DOCKER IMAGE
                if not no_build:
                    build_profile = tag_build_profile_map.get(tag)

                    module_name = build_profile.module_name
                    dockerfile = build_profile.dockerfile
                    self.output.info("BUILDING MODULE: {0}".format(module_name))
                    self.output.info("PROCESSING DOCKERFILE: {0}".format(dockerfile))
                    self.output.info("BUILDING DOCKER IMAGE: {0}".format(tag))

                    build_options = build_profile.extra_options
                    build_options_parser = BuildOptionsParser(build_options)
                    sdk_options = build_options_parser.parse_build_options()

                    context_path = build_profile.context_path

                    # a hack to workaround Python Docker SDK's bug with Linux container mode on Windows
                    dockerfile_relative = os.path.relpath(dockerfile, context_path)
                    if docker.get_os_type() == "linux" and sys.platform == "win32":
                        dockerfile_relative = dockerfile_relative.replace("\\", "/")

                    build_args = {"tag": tag, "path": context_path, "dockerfile": dockerfile_relative}
                    build_args.update(sdk_options)

                    response = docker.docker_api.build(**build_args)
                    docker.process_api_response(response)
                if not no_push:
                    docker.init_registry()

                    # PUSH TO CONTAINER REGISTRY
                    self.output.info("PUSHING DOCKER IMAGE: " + tag)
                    registry_key = None
                    for key, registry in self.envvars.CONTAINER_REGISTRY_MAP.items():
                        # Split the repository tag in the module.json (ex: Localhost:5000/filtermodule)
                        if registry.server.lower() == tag.split('/')[0].lower():
                            registry_key = key
                            break
                    if registry_key is None:
                        self.output.info("Could not find registry credentials with name {0} in environment variable. Pushing anonymously.".format(tag.split('/')[0].lower()))
                        response = docker.docker_client.images.push(repository=tag, stream=True)
                    else:
                        response = docker.docker_client.images.push(repository=tag, stream=True, auth_config={
                            "username": self.envvars.CONTAINER_REGISTRY_MAP[registry_key].username,
                            "password": self.envvars.CONTAINER_REGISTRY_MAP[registry_key].password})
                    docker.process_api_response(response)
            self.output.footer("BUILD COMPLETE", suppress=no_build)
            self.output.footer("PUSH COMPLETE", suppress=no_push)
        self.utility.set_config(force=True, replacements=replacements)

    def _update_launch_json(self, name, template, group_id):
        new_launch_json = self._get_launch_json(name, template, group_id)
        if new_launch_json is not None:
            self._merge_launch_json(new_launch_json)

    def _get_launch_json(self, name, template, group_id):
        replacements = {}
        replacements["%MODULE%"] = name
        replacements["%MODULE_FOLDER%"] = name

        launch_json_file = None
        is_function = False
        if template == "c":
            launch_json_file = "launch_c.json"
            replacements["%APP_FOLDER%"] = "/app"
        elif template == "csharp":
            launch_json_file = "launch_csharp.json"
            replacements["%APP_FOLDER%"] = "/app"
        elif template == "java":
            launch_json_file = "launch_java.json"
            replacements["%GROUP_ID%"] = group_id
        elif template == "nodejs":
            launch_json_file = "launch_node.json"
        elif template == "csharpfunction":
            launch_json_file = "launch_csharp.json"
            replacements["%APP_FOLDER%"] = "/app"
            is_function = True
        elif template == "python":
            launch_json_file = "launch_python.json"
            replacements["%APP_FOLDER%"] = "/app"

        if launch_json_file is not None:
            launch_json_file = os.path.join(os.path.split(__file__)[0], "template", launch_json_file)
            launch_json_content = self.utility.get_file_contents(launch_json_file)
            for key, value in replacements.items():
                launch_json_content = launch_json_content.replace(key, value)
            launch_json = commentjson.loads(launch_json_content)
            if is_function and launch_json is not None and "configurations" in launch_json:
                # for Function modules, there shouldn't be launch config for local debug
                launch_json["configurations"] = list(filter(lambda x: x["request"] != "launch", launch_json["configurations"]))
            return launch_json

    def _merge_launch_json(self, new_launch_json):
        vscode_dir = os.path.join(os.getcwd(), ".vscode")
        self.utility.ensure_dir(vscode_dir)
        launch_json_file = os.path.join(vscode_dir, "launch.json")
        if os.path.exists(launch_json_file):
            launch_json = commentjson.loads(self.utility.get_file_contents(launch_json_file))
            launch_json['configurations'].extend(new_launch_json['configurations'])
            with open(launch_json_file, "w") as f:
                commentjson.dump(launch_json, f, indent=2)
        else:
            with open(launch_json_file, "w") as f:
                commentjson.dump(new_launch_json, f, indent=2)
