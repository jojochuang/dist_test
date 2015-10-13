import datetime
import errno
import os
import logging
import pickle
import shlex, subprocess
import shutil
import tempfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Manifest:
    """Identifies a Maven project based on the git branch.

    Also provides additional information like the git hash
    and when the Manifest was created.
    """

    _FILENAME = ".grind_manifest"
    """Identifying information about a git project."""
    def __init__(self, project_root, git_branch, git_hash, timestamp):
        self.project_root = project_root
        self.git_hash = git_hash
        self.git_branch = git_branch
        self.timestamp = timestamp

    def write(self, output_file):
        with open(output_file, "wt") as o:
            pickle.dump(self, o)

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.project_root == other.project_root and \
                    self.git_branch == other.git_branch
        else:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return "Manifest(%s)" % str(self.__dict__)

    def pretty_str(self):
        strs = []
        if hasattr(self, "project_root"):
            strs.append(self.project_root)
        else:
            strs.append("(Unknown project root)")
        if hasattr(self, "timestamp"):
            strs.append("\tDate: %s" % self.timestamp.strftime("%c"))
        if hasattr(self, "git_branch"):
            strs.append("\tBranch: %s" % self.git_branch)
        if hasattr(self, "git_hash"):
            strs.append("\tHash: %s" % self.git_hash)

        return "\n".join(strs)

    @staticmethod
    def read(input_file):
        logger.debug("Reading manifest at %s", input_file)
        if not os.path.isfile(input_file):
            return None
        with open(input_file, "r") as o:
            return pickle.load(o)

    @staticmethod
    def build_from_project(project_root):
        retcode = subprocess.call("git show-ref --quiet", shell=True, cwd=project_root)
        if retcode != 0:
            raise Exception("Directory %s is not a git repository" % project_root)

        git_hash = subprocess.check_output("git show-ref --head -s HEAD", shell=True, cwd=project_root)
        if git_hash.endswith("\n"):
            git_hash = git_hash[:-1]
        git_branch = subprocess.check_output("git symbolic-ref HEAD", shell=True, cwd=project_root)
        # Trim newlines from the end
        if git_branch.endswith("\n"):
            git_branch = git_branch[:-1]
        return Manifest(os.path.normpath(project_root), git_branch, git_hash, datetime.datetime.now())

class CacheManager:
    """Interface for interacting with cached dependency sets (list, clear, etc)."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir

    def list(self, project_root):
        return Manifest.read(os.path.join(self.cache_dir + project_root, Manifest._FILENAME))

    def list_all(self):
        manifests = list()
        for root, dirs, files in os.walk(self.cache_dir):
            for f in files:
                if f == Manifest._FILENAME:
                    manifests.append(Manifest.read(os.path.join(root, f)))
        return manifests

    def clear(self, project_root):
        shutil.rmtree(self.cache_dir + project_root)

    def clear_all(self):
        shutil.rmtree(self.cache_dir)

class Packager:
    """Packages the dependencies to run tests of a Maven project into an output folder.
    This folder is similar to the project source tree, except it only contains compiled
    artifacts in the target/ directories. Source files are not required to run tests.

    Dependencies come in three types:
        * Dependencies provided by grind. One example is a pinned version of Maven,
          to avoid downloading Maven plugins each time.
        * Built project artifacts, meaning the .jar, test-sources.jar, test.jar, etc.
        * External dependencies from the local Maven repository, e.g. ~/.m2/repository.

    Provided dependencies are in the `skeleton` folder.

    Project artifacts are enumerated from the MavenProject and copied into the
    output folder.

    External dependencies are more complicated. We use the Maven dependency plugin
    to bootstrap a fresh local Maven repository with just the artifacts required
    for the project. However, since this is really slow, we cache the dependency
    set of a Maven project based on the local path to the project and the git branch.

    External dependencies are hardlinked into the output folder, which is more
    efficient than copying.

    Generating the external dependencies for Hadoop can take tens of minutes, but
    only takes seconds when cached.
    """

    # Call it this for familiarity
    _MAVEN_REL_ROOT = ".m2/repository"

    def __init__(self, maven_project, output_root, cache_dir, ignore=None):
        self.__maven_project = maven_project
        self.__project_root = maven_project.project_root
        self.__output_root = output_root
        self.__cache_dir = cache_dir
        if self.__cache_dir is None:
            self.__cache_dir = tempfile.mkdtemp(prefix="grindcache.")
            logger.info("No cache dir specified, using temp directory %s instead", self.__cache_dir)
        self.__cached_project_root = self.__cache_dir + self.__project_root
        self.__manifest = Manifest.build_from_project(self.__project_root)
        if ignore is None:
            #self.__ignore = ["*.jar", "*.war", "surefire-reports"]
            self.__ignore = []
        else:
            self.__ignore = ignore
        self.__test_jars = []
        self.__test_dirs = []
        self.__jars = []

    @staticmethod
    def __mkdirs_recursive(path):
        try:
            os.makedirs(path)
            logger.debug("Created directory %s", path)
        except OSError as exc:
            if exc.errno == errno.EEXIST and os.path.isdir(path):
                pass
            else:
                raise

    def __copy(self, module_path, input_path):
        # module_path is absolute, e.g. /dev/parent-module/sub-module
        # input_path is absolute, e.g. /dev/parent-module/sub-module/target/foo
        assert input_path.startswith(module_path)

        # Form up the output path
        # Get the relpath of the input, join to the output folder
        input_relpath = os.path.relpath(input_path, self.__project_root)
        output_path = os.path.join(self.__output_root, input_relpath)

        # Create the parent directory in the output root if it doesn't exist
        parent_output_path = os.path.dirname(output_path)
        Packager.__mkdirs_recursive(parent_output_path)

        # Copy both files and directories (recursively)
        if os.path.isfile(input_path):
            shutil.copyfile(input_path, output_path)
        elif os.path.isdir(input_path):
            # Do the copy with ignore patterns
            shutil.copytree(input_path, output_path, ignore=shutil.ignore_patterns(*self.__ignore))
        else:
            raise Exception("Cannot copy something that's not a file or directory: " + input_path)

    def _package_target_dirs(self):
        # Copy the pom.xml and also all the test artifacts generated by the project
        # Goal is to create the same target/ directory structure under the output directory
        for module in self.__maven_project.modules:
            self.__copy(module.root, module.pom)
            for artifact in module.test_artifacts:
                self.__copy(module.root, artifact)
                self.__test_jars.append(artifact)
            for artifact in module.source_artifacts:
                self.__copy(module.root, artifact)
                self.__jars.append(artifact)

        # Create some target subdirectories needed by some tests, this happens via antrun in Hadoop
        paths = ("target/test/data", "target/test-dir", "target/log")
        for module in self.__maven_project.modules:
            for p in paths:
                input_abspath = os.path.join(module.root, p)
                module_relpath = os.path.relpath(module.root, self.__project_root)
                dir_relpath = os.path.join(module_relpath, p)
                # Only create if it already exists in source, save us some trouble
                if os.path.exists(input_abspath):
                    self.__test_dirs += [dir_relpath]

        logger.info("Packaged %s modules to output directory %s",\
                    len(self.__maven_project.modules), self.__output_root)


    def _regenerate_dependency_cache_if_necessary(self):
        """Regenerate cached Maven dependencies for a project if the cached dependencies are out of date.
        Cache staleness is determined by matching the project manifest with the cached manifest."""
        cached_project_manifest = os.path.join(self.__cached_project_root, Manifest._FILENAME)
        cached_manifest = Manifest.read(cached_project_manifest)

        if cached_manifest != self.__manifest:
            logger.info("No matching cached dependency set found for path %s manifest %s, regenerating",
                        self.__project_root, cached_manifest)
            # If we found a cached manifest but it didn't match, wipe it first
            if cached_manifest is not None:
                logger.info("Cached manifest %s does not match expected %s", cached_manifest, self.__manifest)
            if os.path.exists(self.__cached_project_root):
                logger.info("Removing stale cached dependency set at %s", self.__cached_project_root)
                shutil.rmtree(self.__cached_project_root)
            # Regenerate dependencies
            self._regenerate_dependency_cache()
            # Write a new manifest
            self.__manifest.write(cached_project_manifest)
            logger.info("Wrote new cache manifest to %s", cached_project_manifest)


    def _regenerate_dependency_cache(self):
        """Regenerate the Maven dependencies for this project.
        This normally happens when the cached dependencies are determined to be stale."""
        # Bootstrap with our skeleton environment
        shutil.copytree(os.path.join(os.path.dirname(__file__), "skeleton"), self.__cached_project_root)

        # Use skeleton environment Maven to copy dependencies into output dir
        cmd = ". %s; which mvn" % os.path.join(self.__cached_project_root, "environment.source")
        logger.info("Detecting environment mvn via `%s`", cmd)
        env_mvn = subprocess.check_output(cmd, shell=True, cwd=self.__cached_project_root)
        if env_mvn.endswith("\n"):
            env_mvn = env_mvn[:-1]

        cached_m2_repo = os.path.join(self.__cached_project_root, Packager._MAVEN_REL_ROOT)

        # copy-dependencies
        cmd = """%s -q dependency:copy-dependencies -Dmdep.useRepositoryLayout=true -Dmdep.copyPom -DoutputDirectory=%s"""
        cmd = cmd % (env_mvn, cached_m2_repo)
        Packager.__shell(cmd, self.__project_root)

        # mvn test without running tests
        cmd = """%s -q -Dmaven.repo.local=%s surefire:test -DskipTests"""
        cmd = cmd % (env_mvn, cached_m2_repo)
        Packager.__shell(cmd, self.__project_root)

        # TODO: add support for specifying additional dependencies not caught by above
        # This is required if we ever want to be able to invoke tests in offline mode.
        # Need to make this generalized, per-project config file?

    @staticmethod
    def __shell(cmd, cwd):
        logger.info("Invoking `%s`", cmd)
        p = subprocess.Popen(shlex.split(cmd), cwd=cwd)
        p.wait()
        if p.returncode != 0:
            raise Exception("Error while invoking %s" % cmd)

    def _package_maven_dependencies(self):
        """Put dependencies from the maven repo into _MAVEN_REL_ROOT in the output directory.
        If the dependencies have been cached from a previous run, hardlink to those instead."""

        # If the project's manifest does not match the manifest of the project's cached dependencies,
        # we need to regenerate the Maven dependencies since they may be out of date.
        self._regenerate_dependency_cache_if_necessary()

        # Hardlink from the cache to the output folder
        logger.info("Linking cached Maven dependencies from %s to %s", self.__cached_project_root, self.__output_root)
        for root, dirs, files in os.walk(self.__cached_project_root):
            for d in dirs:
                fullpath = os.path.join(root, d)
                relpath = os.path.relpath(fullpath, self.__cached_project_root)
                os.mkdir(os.path.join(self.__output_root, relpath))
            for f in files:
                fullpath = os.path.join(root, f)
                relpath = os.path.relpath(fullpath, self.__cached_project_root)
                os.link(fullpath, os.path.join(self.__output_root, relpath))
        logger.info("Finished packaging Maven dependencies in %s", self.__output_root)

    def package_all(self):
        self._package_target_dirs()
        self._package_maven_dependencies()

    @staticmethod
    def get_unzip_cmd(project_root, jar, output_dir):
        jar_relpath = os.path.relpath(jar, project_root)
        parent_relpath = os.path.relpath(os.path.dirname(jar), project_root)
        out_relpath = os.path.join(parent_relpath, output_dir)
        unzip_cmd = "unzip -qq -n %s -d %s" % (jar_relpath, out_relpath)
        return unzip_cmd

    def write_unpack_script(self, name):
        """Unpack the jars to produce classfiles and test resources required for
        running tests. This avoids having to upload and then localize potentially
        thousands of .class files, which takes too long to be useful.
        """

        lines = ["#!/usr/bin/env bash"]
        lines.append("set -e")

        # Extract test and normal jars
        for jar in self.__test_jars:
            lines.append(Packager.get_unzip_cmd(self.__project_root, jar, "test-classes"))
        for jar in self.__jars:
            lines.append(Packager.get_unzip_cmd(self.__project_root, jar, "classes"))

        # Create some extra empty directories
        # Isolate can't handle empty directories, so need to do this in the script.
        for d in self.__test_dirs:
            lines.append("mkdir -p %s" % d)

        # Write the unpack script
        outpath = os.path.join(self.__output_root, name)
        with open(outpath, "wt") as out:
            for line in lines:
                out.write(line)
                out.write("\n")
        os.chmod(outpath, 0755)
        logging.info("Wrote pre-run unpacking script to %s", outpath)

    def get_relative_output_paths(self):
        """Generate relative paths of files in the output directory."""
        paths = []
        for root, dirs, files in os.walk(self.__output_root):
            root_relpath = os.path.relpath(root, self.__output_root)
            for f in files:
                paths += [os.path.join(root_relpath, f)]
        return paths
