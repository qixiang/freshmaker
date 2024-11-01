# -*- coding: utf-8 -*-
# Copyright (c) 2017  Red Hat, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Written by Chenxiong Qi <cqi@redhat.com>
#            Jan Kaluza <jkaluza@redhat.com>
#            Ralph Bean <rbean@redhat.com>

import copy
import re
import requests
import dogpile.cache
import kobo.rpmlib
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby, islice

from freshmaker import log, conf
from freshmaker.kojiservice import koji_service
from freshmaker.odcsclient import create_odcs_client
from freshmaker.pyxis_gql import PyxisGQL
from freshmaker.utils import sorted_by_nvr, is_pkg_modular
from freshmaker.utils import retry
import koji


class ImageGroup:
    def __init__(self, image, pyxis_api_instance):
        parsed_nvr = koji.parse_NVR(image.nvr)
        repositories = image.get_registry_repositories(pyxis_api_instance)
        self.name = parsed_nvr["name"]
        self.version = parsed_nvr["version"]
        self.repos = {x["repository"] for x in repositories}

    def __eq__(self, other):
        return (
            self.name == other.name and self.version == other.version and self.repos == other.repos
        )

    def __str__(self):
        return "%s-%s-%s" % (self.name, self.version, sorted(self.repos))

    def issubset(self, other):
        return (
            self.name == other.name
            and self.version == other.version
            and self.repos.issubset(other.repos)
        )


class KojiLookupError(ValueError):
    """Koji lookup error"""

    pass


class ExtraRepoNotConfiguredError(ValueError):
    """Extra repo required but missing in config"""

    pass


class ContainerRepository(dict):
    """Represent a container repository"""

    @classmethod
    def create(cls, data):
        repo = cls()
        repo.update(data)
        return repo


class ContainerImage(dict):
    """Represent a container image"""

    region = dogpile.cache.make_region().configure(conf.dogpile_cache_backend)

    @classmethod
    def create(cls, data):
        image = cls()
        image.update(data)

        arch = data.get("architecture")
        image["multi_arch_rpm_manifest"] = {}
        rpm_manifest = data.get("rpm_manifest")
        if arch and rpm_manifest:
            image["multi_arch_rpm_manifest"][arch] = rpm_manifest

        return image

    def __hash__(self):
        return hash((self.nvr))

    @property
    def nvr(self):
        return self["brew"]["build"]

    @property
    def is_base_image(self):
        return self["filesystem_koji_task_id"] is not None

    def log_error(self, err):
        """
        Logs the error associated with this image and sets self["error"].
        If there has been previous call of log_error, new `err` is appended
        to self['error'] with ';' separator.
        """
        prefix = ""
        if "brew" in self and "build" in self["brew"]:
            prefix = self.nvr + ": "
        log.error("%s%s", prefix, err)
        if "error" not in self or not self["error"]:
            self["error"] = str(err)
        else:
            self["error"] += "; " + str(err)

    def update_multi_arch(self, image):
        """
        Update multi-arch attributes for this image from another image.

        :param ContainerImage image: the container image object to copy multi
            arch attributes from
        :rtype: None
        """
        image_arch = image.get("architecture")
        if not image_arch:
            return

        image_rpm_manifest = image.get("rpm_manifest")
        if image_rpm_manifest:
            self["multi_arch_rpm_manifest"][image_arch] = image_rpm_manifest

    @staticmethod
    def _get_default_additional_data():
        return {
            "repository": None,
            "commit": None,
            "target": None,
            "git_branch": None,
            "error": None,
            "arches": None,
            "odcs_compose_ids": None,
            "parent_image_builds": None,
            "filesystem_koji_task_id": None,
        }

    @classmethod
    @region.cache_on_arguments()
    def get_additional_data_from_koji(cls, nvr):
        """
        Finds the build defined by `nvr` in Koji and returns dict with
        additional information about this build including "repository",
        "commit", "target" and "git_branch".

        In case of lookup error, the "error" will be set to error string.
        """
        data = cls._get_default_additional_data()

        with koji_service(conf.koji_profile, log, dry_run=conf.dry_run, login=False) as session:
            build = session.get_build(nvr)
            if not build:
                raise KojiLookupError("Cannot find Koji build with nvr %s in Koji" % nvr)

            if "task_id" not in build or not build["task_id"]:
                if (
                    "extra" in build
                    and "container_koji_task_id" in build["extra"]
                    and build["extra"]["container_koji_task_id"]
                ):
                    build["task_id"] = build["extra"]["container_koji_task_id"]
                else:
                    raise KojiLookupError(
                        "Cannot find task_id or container_koji_task_id "
                        "in the Koji build %r" % build
                    )

            fs_koji_task_id = build.get("extra", {}).get("filesystem_koji_task_id")
            if fs_koji_task_id:
                data["filesystem_koji_task_id"] = fs_koji_task_id
                parsed_nvr = koji.parse_NVR(nvr)
                name_version = f'{parsed_nvr["name"]}-{parsed_nvr["version"]}'
                if name_version not in conf.image_extra_repo:
                    msg = (
                        f"{name_version} is a base image, but extra image repo for it "
                        f"is not specified in the Freshmaker configuration."
                    )
                    raise ExtraRepoNotConfiguredError(msg)

            extra_image = build.get("extra", {}).get("image", {})
            # Get the list of ODCS composes used to build the image.
            if extra_image.get("odcs", {}).get("compose_ids"):
                data["odcs_compose_ids"] = extra_image["odcs"]["compose_ids"]

            data["parent_build_id"] = extra_image.get("parent_build_id")
            data["parent_image_builds"] = extra_image.get("parent_image_builds")

            flatpak = extra_image.get("flatpak", False)
            if flatpak:
                data["flatpak"] = flatpak

            brew_task = session.get_task_request(build["task_id"])
            source = brew_task[0]
            data["target"] = brew_task[1]
            extra_data = brew_task[2]
            if "git_branch" in extra_data:
                data["git_branch"] = extra_data["git_branch"]
            else:
                data["git_branch"] = "unknown"

            # Some builds do not have "source" attribute filled in, so try
            # both build["source"] and task_request[0] sources.
            sources = [source]
            if "source" in build:
                sources.insert(0, build["source"])
            for src in sources:
                m = re.match(r".*/(?P<namespace>.*)/(?P<container>.*)#(?P<commit>.*)", src)
                if m:
                    namespace = m.group("namespace")
                    # For some Koji tasks, the container part ends with "?" in
                    # source URL. This is just because some custom scripts for
                    # submitting those builds include this character in source URL
                    # to mark the query part of URL. We need to handle that by
                    # stripping that character.
                    container = m.group("container").rstrip("?")
                    data["repository"] = namespace + "/" + container

                    # There might be tasks which have branch name in
                    # "origin/branch_name" format, so detect it set commit
                    # hash only if this is not true.
                    if "/" not in m.group("commit"):
                        data["commit"] = m.group("commit")
                        break

            if not data["commit"]:
                raise KojiLookupError("Cannot find valid source of Koji build %r" % build)

            if not conf.supply_arch_overrides:
                data["arches"] = None
            else:
                data["arches"] = cls._get_arches_from_koji(session, build["build_id"])

        return data

    @staticmethod
    def _get_arches_from_koji(koji_session, build_id):
        archives = koji_session.list_archives(build_id=build_id)
        arches = [
            archive["extra"]["image"]["arch"] for archive in archives if archive["btype"] == "image"
        ]
        return " ".join(sorted(arches))

    def resolve_commit(self):
        """
        Uses the ContainerImage data to resolve the information about
        commit from which the Docker image has been built.

        Sets the "repository and "commit" keys/values if available.
        """
        # Find the additional data for Container build in Koji.
        try:
            data = self.get_additional_data_from_koji(self.nvr)
        except KojiLookupError as e:
            err = "Cannot get data from Koji for build %s: %s." % (self.nvr, e)
            log.error(err)
            data = self._get_default_additional_data()
            data["error"] = err
        except ExtraRepoNotConfiguredError as e:
            log.error(e)
            data = self._get_default_additional_data()
            data["error"] = str(e)

        self.update(data)

    def resolve_compose_sources(self):
        """
        Get source values of ODCS composes used in image build task
        """
        compose_sources = self.get("compose_sources", None)
        # This has been populated, skip.
        if compose_sources is not None:
            return

        odcs_client = create_odcs_client()
        compose_ids = self.get("odcs_compose_ids")
        if not compose_ids:
            self["compose_sources"] = []
            return

        compose_sources = set()
        for compose_id in compose_ids:
            # Get odcs compose source value from odcs server
            compose = odcs_client.get_compose(compose_id)
            source = compose.get("source", "")
            if source:
                compose_sources.update(source.split())

        self["compose_sources"] = list(compose_sources)
        log.info(
            "Container image %s uses following compose sources: %r",
            self.nvr,
            self["compose_sources"],
        )

    def resolve_content_sets(self, pyxis_api_instance, children=None):
        """
        Find out the content_sets this image uses and store it as
        "content_sets" key in image.

        :param Pyxis pyxis_api_instance: Pyxis instance to use for additional
            queries.
        :param list[ContainerImage] children: List of children to take the
            content_sets from in case this container image is unpublished and
            therefore without "content_sets" set.
        """

        # ContainerImage now has content_sets field, so use it if available.
        if "content_sets" in self and self["content_sets"]:
            log.info(
                "Container image %s uses following content sets: %r", self.nvr, self["content_sets"]
            )
            if "content_sets_source" not in self:
                self["content_sets_source"] = "pyxis_container_image"
            return

        # In case content_sets are not set directly in this ContainerImage,
        # try to get them from children image.
        self["content_sets_source"] = "child_image"
        if not children:
            log.warning(
                "Container image %s does not have 'content_sets' set "
                "in Pyxis and also does not have any children, "
                "this is suspicious.",
                self.nvr,
            )
            self.update({"content_sets": []})
            return

        for child in children:
            # The child['content_sets'] should be always set for children
            # passed here, but in case it is not, just try it.
            if "content_sets" not in child:
                child.resolve(pyxis_api_instance, None)
            if not child["content_sets"]:
                continue

            log.info(
                "Container image %s does not have 'content-sets' set "
                "in Pyxis. Using child image %s content_sets: %r",
                self.nvr,
                child.nvr,
                child["content_sets"],
            )
            self.update({"content_sets": child["content_sets"]})
            return

        log.warning(
            "Container image %s does not have 'content_sets' set "
            "in Pyxis as well as its children, this "
            "is suspicious.",
            self.nvr,
        )
        self.update({"content_sets": []})

    def resolve_published(self, pyxis_api_instance):
        # Get the published version of this image to find out if the image
        # was actually published.
        images = pyxis_api_instance.find_images_by_nvr(self.nvr)
        if not images:
            log.warning("No image %s found in Pyxis.", self.nvr)
            return

        published = any([r["published"] for img in images for r in img["repositories"]])
        if published:
            self["published"] = True
        else:
            self["published"] = False

            # Usually we do not store complete RPM manifest, but when
            # image is unpublished, we need complete RPM manifest in order
            # to check for possible unpublished RPMs.
            # We do not want to get the complete manifest for every container
            # image, because it is relatively big, so fetch it only when needed.
            self["rpm_manifest"] = [{"rpms": images[0]["edges"]["rpm_manifest"]["data"]["rpms"]}]

    def resolve(self, pyxis_api_instance, children=None):
        """
        Resolves the Container image - populates additional metadata by
        querying Koji and Pyxis.
        """
        try:
            log.debug("Resolving image: %s", self.nvr)
            self.resolve_commit()
            self.resolve_compose_sources()
            self.resolve_content_sets(pyxis_api_instance, children)
            self.resolve_published(pyxis_api_instance)
        except Exception as e:
            err = "Cannot resolve the container image: %s" % e
            self.log_error(err)

    def get_rpms(self):
        """
        Extracts the RPMs from the Container image.
        """
        if "rpm_manifest" not in self or not self["rpm_manifest"]:
            # Do not filter if we are not sure what RPMs are in the image.
            log.info(
                ("Not filtering out this image because we " "are not sure what RPMs are in there.")
            )
            return
        # There is always just single "rpm_manifest". Pyxis returns
        # this as a list, because it is reference to
        # containerImageRPMManifest.
        rpm_manifest = self["rpm_manifest"][0]
        if "rpms" not in rpm_manifest:
            # Do not filter if we are not sure what RPMs are in the image.
            log.info(
                ("Not filtering out this image because we " "are not sure what RPMs are in there.")
            )
            return
        return rpm_manifest["rpms"]

    def get_registry_repositories(self, pyxis_api_instance):
        if self["repositories"]:
            return self["repositories"]

        parsed_nvr = kobo.rpmlib.parse_nvr(self.nvr)

        if "." not in parsed_nvr["release"]:
            log.debug("There are no repositories for %s", self.nvr)
            return []

        original_release = parsed_nvr["release"].rsplit(".", 1)[0]
        parsed_nvr["release"] = original_release
        original_nvr = "{name}-{version}-{release}".format(**parsed_nvr)
        log.debug("Finding repositories for %s through %s", self.nvr, original_nvr)

        previous_images = pyxis_api_instance.get_images_by_nvrs(
            [original_nvr], published=None, include_rpm_manifest=False
        )
        if not previous_images:
            log.warning("original_nvr %s not found in Pyxis", original_nvr)
            return []

        return previous_images[0].get_registry_repositories(pyxis_api_instance)


class PyxisAPI(object):
    """Interface to query Pyxis"""

    region = dogpile.cache.make_region().configure(conf.dogpile_cache_backend, expiration_time=120)

    def __init__(self, server_url):
        """Initialize PyxisAPI instance

        :param str server_url: Pyxis GraphQL url
        """
        self.server_url = server_url
        self.pyxis = PyxisGQL(url=server_url, cert=(conf.pyxis_certificate, conf.pyxis_private_key))

    def _dicts_to_images(self, image_dicts):
        """Convert image dictionaries to list of ContainerImage"""

        images = []
        nvr_to_arches = {}
        for image_data in image_dicts:
            image = ContainerImage.create(image_data)
            images.append(image)

            # TODO: In the future, we may want to combine different ContainerImage
            # objects into a single object. For now, ensure that whichever object
            # is used by caller contains multi-arch information.
            nvr = image.nvr
            nvr_to_arches.setdefault(nvr, [])
            nvr_to_arches[nvr].append(image)
            for arch_image in nvr_to_arches[nvr][:-1]:
                arch_image.update_multi_arch(image)
                image.update_multi_arch(arch_image)

        # There can be multi-arch images which share the same
        # image['brew']['build']. Freshmaker is not interested in the image
        # architecture, it is only interested in NVR, so group the images
        # by the same image['brew']['build'] and include just first one in the
        # image list.
        sorted_images = sorted_by_nvr(images, reverse=True)
        images = []

        # We must combine content_sets with same image NVR
        # but different architectures into one content_sets field
        for k, temp_images in groupby(sorted_images, key=lambda item: item.nvr):
            temp_images = list(temp_images)
            img = temp_images[0]
            if "content_sets" in img and len(temp_images) > 1:
                new_content_sets = set(img.get("content_sets"))
                for i in temp_images[1:]:
                    new_content_sets.update(i.get("content_sets", []))
                img["content_sets"] = list(new_content_sets)
            images.append(img)

        return images

    def find_repositories(
        self,
        published=True,
        release_categories=conf.container_release_categories,
        auto_rebuild=True,
        names: list[str] | None = None,
    ):
        """
        Returns dict with repository name as key and ContainerRepository as
        value.

        :param bool published: whether to limit queries to published
            repositories
        :param release_categories: filter only repositories with specific
            release categories (options: Deprecated, Generally Available, Beta,
            Tech Preview)
        :type release_categories: tuple[str] or list[str]
        :param names: names of the repos to limit the search to (optional)
        :rtype: dict
        :return: Dict with repository name as key and ContainerRepository as
            value.
        """
        repositories = self.pyxis.find_repositories(
            published=published, release_categories=release_categories, names=names
        )

        # If the query is for published images, add configurable repos for
        # unpublished images(like EUS) too because they shouldn't be ignored
        if published is True and conf.unpublished_exceptions:
            unpublished_repositories = self.pyxis.find_repositories_by_registry_paths(
                conf.unpublished_exceptions
            )
            repositories.extend(unpublished_repositories)

        repos = []
        for repo_data in repositories:
            if auto_rebuild and not repo_data.get("auto_rebuild_tags"):
                log.info(
                    '"auto_rebuild_tags" not set for %s repository, ignoring repository',
                    repo_data["repository"],
                )
                continue
            repo = ContainerRepository()
            repo.update(repo_data)
            repos.append(repo)

        return {r["repository"]: r for r in repos}

    def filter_out_images_with_higher_rpm_nvr(self, images, rpm_name_to_nvrs):
        """
        Checks whether the input NVRs defined in `rpm_name_to_nvrs` dict are
        newer than the matching RPM NVRs in the container image.

        If all the RPM NVRs in the container image are newer than matching
        input NVRs, the container image is filtered out from the `images`
        list.

        For example: The httpd-2.4-1 RPM is released together with
        httpd-container. In this case, Freshmaker would try to rebuild
        httpd-container, because it contains httpd package. But this is not
        needed, because latest httpd-container already contains that updated
        package. Therefore we filter it out in this method.

        :param list images: List of ContainerImage instances.
        :param dict rpm_name_to_nvrs: Dict with binary RPM name as a key and
            list of NVRs as a value.
        :rtype: list
        :return: List of ContainerImage instances without the filtered images.
        """
        ret = []
        for image in images:
            rpms = image.get_rpms()
            if rpms is None:
                ret.append(image)
            image_included = False
            for rpm in rpms or []:
                image_rpm_nvra = kobo.rpmlib.parse_nvra(rpm["nvra"])
                for rpm_nvr in rpm_name_to_nvrs.get(rpm.get("name"), []):
                    input_rpm_nvr = kobo.rpmlib.parse_nvr(rpm_nvr)
                    # compare_nvr return values:
                    #   - nvr1 newer than nvr2: 1
                    #   - same nvrs: 0
                    #   - nvr1 older: -1
                    # We want to rebuild only images with RPM NVR lower than
                    # input RPM NVR, therefore we check for -1.
                    if (
                        kobo.rpmlib.compare_nvr(image_rpm_nvra, input_rpm_nvr, ignore_epoch=True)
                        == -1
                    ):
                        ret.append(image)
                        image_included = True
                        break
                if image_included:
                    break
            else:
                # Oh-no, the mighty for/else block!
                # The else clause executes after the loop completes normally.
                # This means that the loop did not encounter a break statement.
                # In our case, this means that we filtered out the image.
                log.info(
                    "Will not rebuild %s, because it does not contain "
                    "older version of any input package: %r"
                    % (image.nvr, rpm_name_to_nvrs.values())
                )
        return ret

    def filter_out_modularity_mismatch(self, images, rpm_name_to_nvrs):
        """
        Filter out container images which have a modularity mismatch with ``rpm_name_to_nvrs``.

        If an advisory has a modular RPM, then the container image's RPM of the same name should
        also be modular. The opposite should also be true. If not, the container image is filtered
        out from the ``images`` list.

        :param list images: List of ContainerImage instances.
        :param dict rpm_name_to_nvrs: Dict with RPM name as a key and list
            of NVRs as a value.
        :rtype: list
        :return: List of ContainerImage instances without the filtered images.
        """
        ret = []
        for image in images:
            rpms = image.get_rpms()
            if rpms is None:
                ret.append(image)
            image_included = False
            # Include the image if the RPM from the advisory is modular, and the RPM of the same
            # name in the image is also modular. Also, include the image if the opposite is true.
            for rpm in rpms or []:
                for rpm_nvr in rpm_name_to_nvrs.get(rpm.get("name"), []):
                    if is_pkg_modular(rpm_nvr) == is_pkg_modular(rpm["nvra"]):
                        ret.append(image)
                        image_included = True
                        break
                if image_included:
                    break
            else:
                log.info(
                    "Filtered out %s because there is a modularity mismatch between the RPMs "
                    "from the image and the advisory: %r" % (image.nvr, rpm_name_to_nvrs.values())
                )
        return ret

    def filter_out_images_based_on_content_set(self, images, content_sets):
        """
        Filter out container images based on the content_set.

        Freshmaker queries Pyxis to get images containing affected RPMs installed from a
        particular content_set. At the same time Freshmaker asks to Pyxis also all the images
        with enabled the auto_rebuild_tags tag (when not enabled the rebuilds of images in this
        repository are disabled).
        This gets done only because the Pyxis query will be easier and cleaner this way.
        But because of that some images returned by that query will not have the correct
        content_sets, for this reason we need to filter out images based on the content_sets.

        :param list images: List of ContainerImage instances.
        :param set content_sets: List of content_sets the image includes RPMs
            from.
        :rtype: list
        :return: List of ContainerImage instances without the filtered images.
        """
        ret = []
        for image in images:
            if not content_sets & set(image["content_sets"]):
                log.info(
                    f"Will not rebuild {image.nvr} because its content_sets "
                    "({image['content_sets']}) are not related to the requested content_sets"
                    " ({content_sets})"
                )
            else:
                ret.append(image)
        return ret

    @retry(wait_on=requests.exceptions.ConnectionError, logger=log)
    def find_images_with_included_rpms(
        self, content_sets, rpm_nvrs, repositories, published=True, include_rpm_manifest=True
    ):
        """
        Query Pyxis and find the containerImages in the given containerRepositories.

        By default, limit this only to images which have been published to at least one repository
        and have an auto-rebuild tag.

        If the same image is built for multiple arches, then only one of the arches will be
        returned.

        :param list content_sets: List of content_sets the image includes RPMs
            from.
        :param list rpm_nvrs: list of binary RPM NVRs to look for
        :param dict repositories: List of repository names to look for.
        :param bool published: whether to limit queries to published
            repositories
        :param bool include_rpm_manifest: whether to include the RPMs in the result.
        """
        auto_rebuild_tags = set()
        for repo in repositories.values():
            auto_rebuild_tags |= set(repo["auto_rebuild_tags"])

        # Pyxis cannot compare NVRs, so just ask for all the container
        # images with any version/release of RPM we are interested in and
        # compare it on client side.
        rpm_name_to_nvrs = {}
        for rpm_nvr in rpm_nvrs:
            name = koji.parse_NVR(rpm_nvr)["name"]
            rpm_name_to_nvrs.setdefault(name, []).append(rpm_nvr)

        images = self.pyxis.find_images_by_installed_rpms(
            rpm_name_to_nvrs, content_sets, repositories, published, auto_rebuild_tags
        )
        if not images:
            return []

        # Skip images without Brew metadata. Images built and released by Konflux
        # lack Brew metadata. We don't support rebuilding such images.
        images = [x for x in images if x["brew"]]

        # Avoid manipulating the images directly, use a copy instead
        image_dicts = copy.deepcopy(images)
        del images

        for image in image_dicts:
            # modify Pyxis image data to simulate the data structure returned from LightBlue
            rpms = [
                rpm
                for rpm in image["edges"]["rpm_manifest"]["data"]["rpms"]
                if rpm["name"] in rpm_name_to_nvrs
            ]
            image["rpm_manifest"] = [{"rpms": rpms}]
            del image["edges"]

        # convert the dicts to list of ContainerImage
        images = self._dicts_to_images(image_dicts)

        # The image_request returns container images which are in the
        # right repository and are latest in *some* repository. But we need
        # those images to be latest in one of the `repositories`. It is not
        # trivial to generate LB query like this, so filter this client-side
        # for now.
        image_nvr_to_image = {}
        for image in images:
            nvr = image.nvr
            if nvr in image_nvr_to_image:
                # This image for another architecture has already been seen
                continue

            for repository in image["repositories"]:
                if repository["repository"] not in repositories:
                    continue

                # skip images from build repositories
                if repository["registry"] in conf.image_build_repository_registries:
                    continue

                published_repo = repositories[repository["repository"]]
                tag_names = [tag["name"] for tag in repository["tags"]]

                for auto_rebuild_tag in published_repo["auto_rebuild_tags"]:
                    if auto_rebuild_tag in tag_names:
                        image["release_categories"] = published_repo["release_categories"]
                        # There can potentially be multiple published
                        # repositories but we only store the first one we
                        # encounter. This adds uncertainty, but it's good
                        # enough for our current use case.
                        image["published_repo"] = repository["repository"]
                        image_nvr_to_image[nvr] = image
                        break
                else:
                    # If no match is found, continue to the next repository
                    continue

                # If a match was found, continue to the next image
                break

        # Reassign the filtered values to `images`
        images = list(image_nvr_to_image.values())
        images = self.filter_out_images_with_higher_rpm_nvr(images, rpm_name_to_nvrs)
        images = self.filter_out_modularity_mismatch(images, rpm_name_to_nvrs)
        if content_sets:
            images = self.filter_out_images_based_on_content_set(images, set(content_sets))
        return images

    def get_images_by_nvrs(
        self,
        nvrs,
        published=True,
        content_sets=None,
        rpm_nvrs=None,
        include_rpm_manifest=True,
        rpm_names=None,
        pyxis_api_instance=None,
    ):
        """Query Pyxis and returns containerImages defined by list of
        `nvrs`.

        :param list nvrs: List of NVRs defining the containerImages to return.
        :param bool published: whether to limit queries to published images
        :param list content_sets: List of content_sets the image includes RPMs
            from.
        :param list rpm_nvrs: list of binary RPM NVRs to look for
        :param bool include_rpm_manifest: When True, the rpm_manifest is
            included in the returned ContainerImages.
        :param list rpm_names: list of RPM names to look for.
        :param PyxisGQL pyxis_api_instance: an instance of PyxisGQL
        :return: List of containerImages.
        :rtype: list of ContainerImages.
        """
        if pyxis_api_instance is None:
            pyxis_api_instance = self.pyxis

        if len(nvrs) == 1:
            images = pyxis_api_instance.find_images_by_nvr(
                nvrs[0], include_rpms=include_rpm_manifest
            )
        else:
            images = pyxis_api_instance.find_images_by_nvrs(nvrs, include_rpms=include_rpm_manifest)
        if not images:
            return []

        # Avoid manipulating the images directly, uses a copy instead.
        image_dicts = copy.deepcopy(images)
        del images

        for image in image_dicts:
            # modify Pyxis image data to simulate the data structure returned from LightBlue
            image["rpm_manifest"] = [copy.deepcopy(image["edges"]["rpm_manifest"]["data"])]
            del image["edges"]

        if content_sets is not None:
            # Filter out images that don't have any of the content sets
            image_dicts = [
                d for d in image_dicts if not set(content_sets).isdisjoint(set(d["content_sets"]))
            ]

        def _image_has_rpm(image, rpms):
            try:
                # rpms is a list of rpm names
                rpm_names = [x["name"] for x in image["rpm_manifest"][0]["rpms"]]
            except Exception:
                # When this is called, the image should have rpm_manifest, but Pyxis
                # may have some invalid image data which is missing rpm_manifest; when
                # such images are returned by Pyxis, it will trigger an exception; we need
                # to log the image NVR for troubleshooting purposes.
                log.error("Invalid image metadata for image %s", image["brew"]["build"])
                raise

            # return True if image has any of the rpms installed
            return not set(rpms).isdisjoint(set(rpm_names))

        if rpm_nvrs is not None:
            # Pyxis cannot compare rpm NVRs, so just ask for all the container
            # images with any version/release of RPM we are interested in and
            # compare it on client side.
            rpm_name_to_nvrs = {}
            for rpm_nvr in rpm_nvrs:
                name = koji.parse_NVR(rpm_nvr)["name"]
                rpm_name_to_nvrs.setdefault(name, []).append(rpm_nvr)

            image_dicts = list(
                filter(lambda x: _image_has_rpm(x, rpm_name_to_nvrs.keys()), image_dicts)
            )

        if rpm_names:
            image_dicts = list(filter(lambda x: _image_has_rpm(x, rpm_names), image_dicts))

        if published is not None:

            def _image_visibility_is(image, published):
                # published: boolean value, True or False
                for repo in image["repositories"]:
                    if repo["registry"] in conf.image_build_repository_registries:
                        continue
                    if repo["published"] is published:
                        return True
                return False

            image_dicts = list(filter(lambda x: _image_visibility_is(x, published), image_dicts))

        images = self._dicts_to_images(image_dicts)

        if rpm_nvrs is not None:
            images = self.filter_out_images_with_higher_rpm_nvr(images, rpm_name_to_nvrs)
        return images

    def get_images_by_brew_package(self, names):
        """
        Query Pyxis to get all the images for a specific list of names.
        :param names list: list of names we want to find images for.
        :return: list of container images matching the requested names.
        :rtype: list of ContainerImages
        """
        images = self.pyxis.find_images_by_names(names)
        return self._dicts_to_images(images)

    def find_images_by_nvr(self, nvr):
        """
        Query Pyxis to get images of NVR

        :param str nvr: image NVR
        :rtype: list of dict
        :return: list of images returned by Pyxis
        """
        return self.pyxis.find_images_by_nvr(nvr)

    def find_parent_brew_build_nvr_from_child(self, child_image, pyxis_api_instance=None):
        """
        Returns the parent brew build NVR of the input image. If the parent is not found it returns None.

        :param ContainerImage child_image: ContainerImage object, image for which we need to find the parent.
        :param PyxisGQL pyxis_api_instance: an instance of PyxisGQL

        :return: parent brew build NVR of the input image.
        :rtype: str

        """
        if pyxis_api_instance is None:
            pyxis_api_instance = self.pyxis

        parent_brew_build = child_image.get("parent_brew_build")
        if parent_brew_build:
            return parent_brew_build
        # We need to resolve the image in here because "parent_image_builds" needs to be there
        # and it gets populated when the image gets resolved.
        child_image.resolve(pyxis_api_instance)

        # Some images have `parent_image_builds` but are built in a multi-stage way and don't have
        # `parent_build_id`, in this situation FM doesn't try to find the parent image and skip.
        if not child_image.get("parent_build_id"):
            return None
        # If the parent is not in `parent_brew_build` we can try to look for the parent in Brew,
        # using the field `parent_image_builds` (searching for the nvr), which should always be there.
        # In case parent_brew_build is None and child_image["parent_image_builds"] == {},
        # it means we found a base image and there's no parent image.
        if child_image["parent_image_builds"]:
            parent_brew_build = [
                i["nvr"]
                for i in child_image["parent_image_builds"].values()
                if i["id"] == child_image["parent_build_id"]
            ][0]

        return parent_brew_build

    def find_parent_images_with_package(
        self, child_image, rpm_name, images=None, pyxis_api_instance=None
    ):
        """
        Returns the chain of all parent images of the image which contain the
        package `rpm_name` in their RPM manifest.

        The first item in the list is the direct parent of the image in question.
        The last item in the list is the top level parent of the image in
        question.

        This method is recursive.
        """
        if pyxis_api_instance is None:
            pyxis_api_instance = self.pyxis

        if not images:
            images = []
        parent_image = None

        # We first try to find the parent from the `parent_brew_build` field in Pyxis.
        parent_brew_build = self.find_parent_brew_build_nvr_from_child(
            child_image, pyxis_api_instance
        )
        # We've reached the base image, stop recursion
        if not parent_brew_build:
            return images
        parent_image = self.get_images_by_nvrs(
            [parent_brew_build],
            rpm_names=[rpm_name],
            published=None,
            pyxis_api_instance=pyxis_api_instance,
        )

        if parent_image:
            # In some cases, an image may not have its content sets defined. To
            # circumvent this gap, we use the list of child images when calling
            # resolve so their content sets can be used.
            children = images if images else [child_image]
            parent_image = parent_image[0]
            parent_image.resolve(pyxis_api_instance, children)

        if images:
            if parent_image:
                images[-1]["parent"] = parent_image
            else:
                # If we did not find the parent image with the package,
                # we still want to set the parent of the last image with
                # the package so we know against which image it has been
                # built.
                # Let's try first with the "parent_brew_build" field.
                parent = self.get_images_by_nvrs(
                    [parent_brew_build], published=None, pyxis_api_instance=pyxis_api_instance
                )
                if parent:
                    parent = parent[0]
                    parent.resolve(pyxis_api_instance, images)
                else:
                    err = "Couldn't find parent image %s. Pyxis data is probably incomplete" % (
                        parent_brew_build
                    )
                    log.error(err)
                    if not images[-1]["error"]:
                        images[-1]["error"] = err
                images[-1]["parent"] = parent

        if not parent_image:
            return images
        images.append(parent_image)
        return self.find_parent_images_with_package(
            parent_image, rpm_name, images, pyxis_api_instance
        )

    def find_images_with_packages_from_content_set(
        self,
        rpm_nvrs,
        content_sets,
        filter_fnc=None,
        published=True,
        release_categories=conf.container_release_categories,
        leaf_container_images=None,
        repositories: list[str] | None = None,
    ):
        """Query Pyxis and find containers which contain given
        package from one of content sets

        :param list rpm_nvrs: list of binary RPM NVRs to look for
        :param list content_sets: list of strings (content sets) to consider
            when looking for the packages
        :param function filter_fnc: Function called as
            filter_fnc(container_image) with container_image being
            ContainerImage instance. If this function returns True, the image
            will not be considered for a rebuild as well as its parent images.
            This function is used to filter out images not allowed by
            Freshmaker configuration.
        :param bool published: whether to limit queries to published
            repositories
        :param release_categories: filter only repositories with specific
            release categories (options: Deprecated, Generally Available, Beta, Tech Preview)
        :type release_categories: tuple[str] or list[str]
        :param list leaf_container_images: List of NVRs of leaf images to
            consider for the rebuild. If not set, all images found in
            Pyxis will be considered for rebuild.
        :param repositories: Repos to restrict the search to (Optional)
        :type repositories: list of str

        :return: a list of dictionaries which represents container images
        :rtype: list
        """

        repos = self.find_repositories(published, release_categories, names=repositories)

        if not repos:
            return []
        if not leaf_container_images:
            images = []
            # Split the repos to small chunks to generate "small" queries
            repos_iterator = iter(repos)
            chunk_size = 50
            for i in range(0, len(repos), chunk_size):
                repos_chunk = {k: repos[k] for k in islice(repos_iterator, chunk_size)}
                images_chunk = self.find_images_with_included_rpms(
                    content_sets, rpm_nvrs, repos_chunk, published
                )
                if images_chunk:
                    images.extend(images_chunk)
        else:
            # The `leaf_container_images` can contain unpublished container image,
            # therefore set `published` to None.
            images = self.get_images_by_nvrs(leaf_container_images, None, content_sets, rpm_nvrs)

        # In case we query for unpublished images, we need to return just
        # the latest NVR for given name-version, otherwise images would
        # contain all the versions which ever containing the rpm_name.
        if not published:

            def _name_version_key(item):
                nvr = koji.parse_NVR(item.nvr)
                return f"{nvr['name']}-{nvr['version']}"

            images = [
                next(grouped_images)
                for _, grouped_images in groupby(
                    sorted_by_nvr(images, reverse=True), key=_name_version_key
                )
            ]

        # Filter out images based on the filter_fnc.
        if filter_fnc:
            images = [image for image in images if not filter_fnc(image)]

        def _resolve_image(image):
            # We do not set "children" here in resolve_content_sets call, because
            # published images should have the content_set set.
            pyxis_api_instance = PyxisGQL(
                url=self.server_url, cert=(conf.pyxis_certificate, conf.pyxis_private_key)
            )
            image.resolve(pyxis_api_instance, None)

            # Mark as latest_released only images which are not Beta or Tech Preview.
            # This is important, because "latest_released" is used in deduplication
            # code to mark the image to which the other images with same name-version
            # but lower release can be upgraded.
            release_categories = image.get("release_categories", [])
            if "Beta" not in release_categories and "Tech Preview" not in release_categories:
                image["latest_released"] = True
            image["directly_affected"] = True
            return image

        with ThreadPoolExecutor(max_workers=conf.max_thread_workers) as executor:
            return list(executor.map(_resolve_image, images))

    def _deduplicate_images_to_rebuild(self, to_rebuild):
        """
        Deduplicates the images to rebuild in `to_rebuild` in-place.

        The `to_rebuild` list is a list in following format:
            [
                [child_image, parent_of_child_image, parent_of_parent, ...],
                ...
            ]

        This methods goes through all the images in `to_rebuild` list and
        changes the list in a way that only single image with the highest
        release will exist for the given image name-version.

        For example, if there are three images in a list - foo-1-2, foo-1-3
        and foo-2-2, the foo-1-3 will be used instead of foo-1-2 on every
        occurrence in a list, because the NVR is higher than NVR of foo-1-2.
        The foo-2-2 will be kept unchanged in a list, because it is the
        single record for the foo image in version 2.
        """

        # We need to deduplicate images in two phases:
        #
        # 1) "handle_parent_change" - During this phase, we find out if update
        #    to latest image changes also the parent images.
        #    For example, foo-1-1 can be built against x-1-1, but foo-1-2 can
        #    be built against y-1-1. If we simply replace "foo-1-1" by "foo-1-2"
        #    while keeping the original parent image, the "foo-1-2" will be built
        #    against x-1-1 instead of y-1-1. This would be wrong.
        #
        #    To fix that, we therefore find out that the parent image changed in
        #    the latest release of foo-1-2 and we replace also the parent images
        #    according to latest release foo-1-2.
        #
        # 2) "update_to_latest". During this phase, we simply find out old releases
        #    of images in `to_rebuild` and update them to latest released NVR.
        for phase in ["handle_parent_change", "update_to_latest"]:
            # Temporary dict mapping the NVR of image to coordinates in the
            # `to_rebuild` list. For example
            # nvr_to_coordinates["nvr"] = [[0, 3], ...] means that the image with
            # nvr "nvr" is 4th image in the to_rebuild[0] list, ...
            nvr_to_coordinates = {}
            # Temporary dict mapping the NV-repository_key to list of NVRs.
            # The List of NVRs is always sorted descending.
            image_group_to_nvrs = {}
            # Temporary dict mapping the NVR to image.
            nvr_to_image = {}
            # Temporary dict mapping image_group to latest released NVR for that image_group.
            image_group_to_latest_released_nvr = {}

            # Constructs the temporary dicts as described above.
            for image_id, images in enumerate(to_rebuild):
                for parent_id, image in enumerate(images):
                    image_group = str(self.describe_image_group(image))
                    image_group_to_nvrs.setdefault(image_group, [])
                    if image.nvr not in image_group_to_nvrs[image_group]:
                        image_group_to_nvrs[image_group].append(image.nvr)

                    nvr_to_coordinates.setdefault(image.nvr, []).append([image_id, parent_id])
                    nvr_to_image[image.nvr] = image

                    if image.get("latest_released"):
                        image_group_to_latest_released_nvr[image_group] = image.nvr

            # Sort the lists in image_group_to_nvrs dict.
            for image_group in image_group_to_nvrs.keys():
                image_group_to_nvrs[image_group] = sorted_by_nvr(
                    image_group_to_nvrs[image_group], reverse=True
                )

                # There might be container image NVRs which are not released yet,
                # but some released image is already built on top of them.
                # The issue is that such unreleased container image won't be in
                # its containerRepository and therefore won't have proper
                # content_sets set.
                # In this case, we copy the content_sets from the released image.
                # This might bring issue in case the content_sets changed
                # dramatically between released and unreleased release of such
                # image, but it's still the best guess we can do.
                # This is also used only as fallback in case "content_sets.yml"
                # does not exists in the dist-git repo, which should be rare
                # situation.
                latest_content_sets = []
                for nvr in reversed(image_group_to_nvrs[image_group]):
                    image = nvr_to_image[nvr]
                    if not image.get("content_sets") or "content_sets_source" not in image:
                        image["content_sets"] = latest_content_sets
                    elif image["content_sets_source"] == "child_image":
                        if latest_content_sets:
                            image["content_sets"] = latest_content_sets
                    else:
                        latest_content_sets = image["content_sets"]

            # Iterate through list of NVs.
            for image_group, nvrs in image_group_to_nvrs.items():
                # We want to replace NVRs which are lower than the latest released
                # NVR with latest released NVR. If there are some higher NVRs, we
                # want to keep them, because we don't want to rebuild the image
                # against older NVR than the one it is currently built against.
                if image_group in image_group_to_latest_released_nvr:
                    latest_released_nvr = image_group_to_latest_released_nvr[image_group]
                else:
                    latest_released_nvr = nvrs[0]

                # The latest_released_nvr_index points to the latest released NVR
                # in the `nvrs` list. Because `nvrs` list is desc sorted, every NVR
                # with higher index is lower and therefore we need to replace it.
                if not conf.container_released_dependencies_only:
                    latest_released_nvr_index = nvrs.index(latest_released_nvr)
                else:
                    # In case we want to use only released versions of images,
                    # replace all the images with the latest released one.
                    latest_released_nvr_index = -1

                if phase == "handle_parent_change":
                    # Find out the name of parent image of latest release image.
                    latest_image = nvr_to_image[latest_released_nvr]
                    if not latest_image.get("parent"):
                        continue
                    latest_parent_nvr_dict = koji.parse_NVR(latest_image["parent"].nvr)
                    latest_parent_name = latest_parent_nvr_dict["name"]
                    latest_parent_version = latest_parent_nvr_dict["version"]

                    # Go through the older images and in case the parent image differs,
                    # update its parents according to latest image parents.
                    for nvr in nvrs[latest_released_nvr_index + 1 :]:
                        image = nvr_to_image[nvr]
                        if not image.get("parent"):
                            continue
                        parent_nvr_dict = koji.parse_NVR(image["parent"].nvr)
                        parent_name = parent_nvr_dict["name"]
                        parent_version = parent_nvr_dict["version"]
                        if (parent_name, parent_version) != (
                            latest_parent_name,
                            latest_parent_version,
                        ):
                            for image_id, parent_id in nvr_to_coordinates[nvr]:
                                latest_image_id, latest_parent_id = nvr_to_coordinates[
                                    latest_released_nvr
                                ][0]
                                to_rebuild[image_id][parent_id:] = to_rebuild[latest_image_id][
                                    latest_parent_id:
                                ]
                elif phase == "update_to_latest":
                    for nvr in nvrs[latest_released_nvr_index + 1 :]:
                        for image_id, parent_id in nvr_to_coordinates[nvr]:
                            # At first replace the image in to_rebuild based
                            # on the coordinates from temp dict.
                            to_rebuild[image_id][parent_id] = nvr_to_image[latest_released_nvr]

                            # And in case this image is not the the leaf image, also replace
                            # the ["parent"] record for the child image to point to the image
                            # with highest NVR.
                            if parent_id != 0:
                                to_rebuild[image_id][parent_id - 1]["parent"] = nvr_to_image[
                                    latest_released_nvr
                                ]

        return to_rebuild

    # Cache to avoid multiple calls. We want one call per nvr, not one per arch
    @region.cache_on_arguments(to_str=lambda image: image.nvr)
    def describe_image_group(self, image):
        """
        Takes an image as an arguement and returns the Name-Version-[Repo]
        """
        return ImageGroup(image, self)

    def _images_to_rebuild_to_batches(self, to_rebuild, directly_affected_nvrs):
        """
        Creates batches with images as defined by `find_images_to_rebuild`
        output from the `to_rebuild` list in following format:

            [
                [child_image, parent_of_child_image, parent_of_parent, ...],
                ...
            ]

        :param list to_rebuild: the list of images to rebuild
        :param set directly_affected_nvrs: the set of NVRs that were detected as directly affected
            and that should have `directly_affected` value set.
        :return: a list of batches with each batch having a list of images
        :rtype: list
        """
        # At first get the max length of list in to_rebuild list.
        max_len = 0
        for rebuild_list in to_rebuild:
            max_len = max(len(rebuild_list), max_len)

        # Now create the batches with images. We still might find duplicate
        # images in to_rebuild lists in two cases:
        #
        # 1) A depends on X and also B depends on X. The X then would be
        #    added to first batch twice. This is simple to fix by just
        #    adding same image to batch once.
        # 2) A depends on X and A is also standalone image to rebuild. In this
        #    case, A would be in the second batch, because A must be built
        #    before X, but it is also standalone image to be rebuilt, so it
        #    would appear also in the first batch.
        #    To fix this, we at first add images with the longest dependency
        #    chains, so A will be added to second batch. Once we try to add
        #    standalone version of A, we won't add it, because it already
        #    exists in some batch.
        #
        # Both of these cases are handled by adding the image to `seen` set
        # and checking if it exists there already before adding it again.
        batches = [[] for i in range(max_len)]
        seen = set()
        for image_rebuild_list in sorted(to_rebuild, key=lambda lst: len(lst), reverse=True):
            for image, batch in zip(reversed(image_rebuild_list), batches):
                image_key = image.nvr
                # If one of the parents is directly affected but not marked, mark it explicitly
                if image_key in directly_affected_nvrs and not image.get("directly_affected"):
                    image["directly_affected"] = True
                if image_key in seen:
                    continue
                seen.add(image_key)
                batch.append(image)
        return batches

    def find_images_to_rebuild(
        self,
        rpm_nvrs,
        content_sets,
        published=True,
        release_categories=conf.container_release_categories,
        filter_fnc=None,
        leaf_container_images=None,
        skip_nvrs=None,
        repositories: list[str] | None = None,
    ):
        """
        Find images to rebuild through image build layers

        Returns the list of sub-lists in which each sub-list contains
        ContainerImage instances which can be built in parallel. Sub-list N+1
        contains images which depend on images from sub-list N, so building any
        image from N+1 must happen *after* all of the images from sub-list N
        have been rebuilt.

        :param list rpm_nvrs: List of binary RPM NVRs to look for
        :param list content_sets: list of strings (content sets) to consider
            when looking for the packages
        :param bool published: whether to limit queries to published
            repositories
        :param tuple release_categories: filter only repositories with specific
            release categories (options: Deprecated, Generally Available, Beta, Tech Preview)
        :param function filter_fnc: Function called as
            filter_fnc(container_image) with container_image being
            ContainerImage instance. If this function returns True, the image
            will not be considered for a rebuild as well as its parent images.
            This function is used to filter out images not allowed by
            Freshmaker configuration.
        :param list leaf_container_images: List of NVRs of leaf images to
            consider for the rebuild. If not set, all images found in
            Pyxis will be considered for rebuild. Note that `published`
            is not respected when `leaf_container_images` are used.
        :param list skip_nvrs: List of NVRs of images to be skipped.
        :param repositories: Repos to restrict the search to (Optional)
        :type repositories: list of str
        """
        images = self.find_images_with_packages_from_content_set(
            rpm_nvrs,
            content_sets,
            filter_fnc,
            published,
            release_categories,
            leaf_container_images=leaf_container_images,
            repositories=repositories,
        )

        # Remove any hotfix images from list of images
        for img in images[:]:
            if any(label["name"] == "com.redhat.hotfix" for label in img["parsed_data"]["labels"]):
                images.remove(img)
                log.debug("Excluding hotfix image: %s", img.nvr)

        # Not skip images when rebuild images are requested explicitly
        if skip_nvrs and not leaf_container_images:
            images = [img for img in images if img["brew"]["build"] not in skip_nvrs]

        rpm_names = [koji.parse_NVR(rpm_nvr)["name"] for rpm_nvr in rpm_nvrs]

        def _get_images_to_rebuild(image):
            """
            Find out parent images to rebuild, helper called from threadpool.
            """
            pyxis_api_instance = PyxisGQL(
                url=self.server_url, cert=(conf.pyxis_certificate, conf.pyxis_private_key)
            )

            rebuild_list = {}  # per binary rpm name rebuild list.
            for rpm_name in rpm_names:
                for rpm in image["rpm_manifest"][0]["rpms"]:
                    if rpm["name"] == rpm_name:
                        break
                else:
                    # This `rpm_name` is not in image.
                    continue

                rebuild_list[rpm_name] = self.find_parent_images_with_package(
                    image, rpm_name, images=[], pyxis_api_instance=pyxis_api_instance
                )
                if rebuild_list[rpm_name]:
                    image["parent"] = rebuild_list[rpm_name][0]
                else:
                    parent_brew_build = self.find_parent_brew_build_nvr_from_child(
                        image, pyxis_api_instance
                    )
                    if parent_brew_build:
                        parent = self.get_images_by_nvrs(
                            [parent_brew_build],
                            published=None,
                            pyxis_api_instance=pyxis_api_instance,
                        )
                        if parent:
                            parent = parent[0]
                            parent.resolve(pyxis_api_instance, images)
                            image["parent"] = parent
                rebuild_list[rpm_name].insert(0, image)
            return rebuild_list

        # For every image, find out all its parent images which contain the
        # binary rpm package and store these lists to to_rebuild.
        to_rebuild = []
        optimization_base = 50
        with ThreadPoolExecutor(max_workers=conf.max_thread_workers) as executor:
            for result in executor.map(_get_images_to_rebuild, images):
                to_rebuild.extend(result.values())
                # Memory consumption of fully constructed to_rebuild list could
                # be large. To prevent this we will periodically use
                # deduplication on the list to reduce it size.
                if len(to_rebuild) > optimization_base:
                    self._deduplicate_images_to_rebuild(to_rebuild)
                    optimization_base += 50
        # The to_rebuild list now contains all the images which need to be
        # rebuilt, but there are lot of duplicates there.

        # At first remove duplicated images which share the same name and
        # version, but different release.
        to_rebuild = self._deduplicate_images_to_rebuild(to_rebuild)
        # Get all the directly affected images so that any parents that are not marked as
        # directly affected can be set in _images_to_rebuild_to_batches
        directly_affected_nvrs = {image.nvr for image in images if image.get("directly_affected")}
        # Some images that aren't marked as directly affected may have already been fixed
        # in the latest published version of the image. Use those images instead.
        self._filter_out_already_fixed_published_images(
            to_rebuild, directly_affected_nvrs, rpm_nvrs, content_sets
        )

        # Replace base images that are not the latest and are used as dependency images
        if conf.update_base_image:
            self._replace_base_images(to_rebuild, rpm_nvrs)

        # Now generate batches from deduplicated list and return it.
        return self._images_to_rebuild_to_batches(to_rebuild, directly_affected_nvrs)

    def _filter_out_already_fixed_published_images(
        self, to_rebuild, directly_affected_nvrs, rpm_nvrs, content_sets
    ):
        """
        Replace images in ``to_rebuild`` that are not directly affected and have published fixes.

        When an image and its parents in ``to_rebuild`` are not directly affected, it's possible
        that the image had been rebuilt outside of Freshmaker and published with the fix applied.
        In this case, Freshmaker should not rebuild the image and its parents. The latest published
        image is found by filtering by the same name and version but finding the highest release.
        This approach is seen as slightly less accurate but safer than using the pullspec used
        in the FROM line of the Dockerfile of the child image.

        :param Iterable to_rebuild: the list of images to rebuild; each element is
            an iterable with the first element being the child image and each subsequent
            image being the parent of the previous image
        :param Iterable directly_affected_nvrs: the set of image NVRs in ``to_rebuild`` that are
            marked as directly affected
        :param Iterable rpm_nvrs: the list of RPM NVRs with the fixes in the advisory
        :param Iterable content_sets: the list of content sets that the RPMs in ``rpm_nvrs`` are
            released in
        """
        for image_group in to_rebuild:
            # Find the first index in image_group of an image that is not directly
            # affected with parents that are also not directly affected
            not_directly_affected_index = None
            # Skip the first image in the group since it is always directly affected
            for i, image in enumerate(image_group[1:], start=1):
                if image.nvr in directly_affected_nvrs:
                    not_directly_affected_index = None
                elif not_directly_affected_index is None:
                    not_directly_affected_index = i

            # The image group does not end with one or more images that are not directly affected
            if not_directly_affected_index is None:
                continue

            # Try replacing all the not directly affected images starting from the first one
            for i in range(not_directly_affected_index, len(image_group)):
                parent_image = image_group[i]
                rpm_name_to_nvrs = {kobo.rpmlib.parse_nvr(nvr)["name"]: nvr for nvr in rpm_nvrs}
                # Get the RPM NVRs that were fixed and apply to the parent image since
                # get_fixed_published_image will ensure all those RPMs are present
                parent_applicable_rpm_nvrs = set()
                if not parent_image.get_rpms():
                    log.warning(
                        "The parent image %s does not have an RPM manifest", parent_image.nvr
                    )
                    continue

                for rpm in parent_image.get_rpms():
                    if rpm_name_to_nvrs.get(rpm["name"]):
                        parent_applicable_rpm_nvrs.add(rpm_name_to_nvrs[rpm["name"]])

                parsed_parent_nvr = kobo.rpmlib.parse_nvr(parent_image.nvr)
                fixed_published_image = self.get_fixed_published_image(
                    parsed_parent_nvr["name"],
                    parsed_parent_nvr["version"],
                    self.describe_image_group(parent_image),
                    parent_applicable_rpm_nvrs,
                    content_sets,
                )
                if fixed_published_image:
                    # The index to start replacements at should be set to i.
                    # If this was the first iteration of the for loop, it would
                    # have already been set to this value.
                    not_directly_affected_index = i
                    break
            else:
                # After all that, there is no published image with the fix  :'(
                continue

            log.info(
                "The image %s will be replaced with the latest published image of %s",
                image.nvr,
                fixed_published_image.nvr,
            )
            # On the first iteration, this is the last directly affected image in image_group
            child_image = image_group[i - 1]
            # Replace the parent of child_image with the fixed published parent image
            # and then remove the remaining images after it in `to_rebuild`
            child_image["parent"] = fixed_published_image
            del image_group[not_directly_affected_index:]

    def _replace_base_images(self, to_rebuild, rpm_nvrs):
        """
        Replace base images that are not the latest and are used as dependency images.

        :param Iterable to_rebuild: the list of images to rebuild; each element is
            an iterable with the first element being the child image and each subsequent
            image being the parent of the previous image
        :param Iterable rpm_nvrs: the list of RPM NVRs with the fixes in the advisory
        """
        rpm_name_to_nvrs = {kobo.rpmlib.parse_nvr(nvr)["name"]: nvr for nvr in rpm_nvrs}

        replacements = {}
        for image_group in to_rebuild:
            # Base image can only be present as the last image in list
            image = image_group[-1]

            # Skip non-base images
            if not image.is_base_image:
                continue
            # Skip directly affected images
            if image.get("directly_affected", False):
                continue

            if image.nvr in replacements:
                new_image = replacements[image.nvr]
                if not new_image:
                    continue
                image_group[-1] = new_image
                image_group[-2]["parent"] = new_image
                continue

            parsed_image_nvr = kobo.rpmlib.parse_nvr(image.nvr)
            images = self.pyxis.find_latest_images_by_name_version(
                parsed_image_nvr["name"], parsed_image_nvr["version"], published=True
            )
            if not images:
                continue

            candidate_nvr = images[0]["brew"]["build"]
            parsed_candidate_nvr = kobo.rpmlib.parse_nvr(candidate_nvr)
            # Skip if the latest published NVR <= current NVR
            if kobo.rpmlib.compare_nvr(parsed_candidate_nvr, parsed_image_nvr) < 1:
                replacements[image.nvr] = None
                continue

            # Now that the latest base image to be used as replacement is determined,
            # get it from pyxis with all the metadata required by Freshmaker
            images = self.pyxis.find_images_by_nvr(candidate_nvr)
            if not images:
                log.error("Image not found: %s", candidate_nvr)
                continue

            images = self.postprocess_images(images, rpm_name_to_nvrs)
            new_image = images[0]
            new_image.resolve(self)

            image_group[-1] = new_image
            image_group[-2]["parent"] = new_image
            replacements[image.nvr] = new_image

    def postprocess_images(self, images, rpm_name_to_nvrs):
        # Avoid manipulating the images directly, uses a copy instead.
        image_dicts = copy.deepcopy(images)
        del images

        for image in image_dicts:
            # modify Pyxis image data to simulate the data structure returned from LightBlue
            rpms = [
                rpm
                for rpm in copy.deepcopy(image["edges"]["rpm_manifest"]["data"]["rpms"])
                if rpm["name"] in rpm_name_to_nvrs.keys()
            ]
            image["rpm_manifest"] = [{"rpms": rpms}]
            del image["edges"]

        # convert the dicts to list of ContainerImage
        return self._dicts_to_images(image_dicts)

    @region.cache_on_arguments()
    def get_fixed_published_image(self, name, version, image_group, rpm_nvrs, content_sets):
        """
        Find a published image with the name, version, and patched RPMs.

        Rather than pass in the original image as a `ContainerImage` object, separate primitives
        are used to make caching better.

        :param str name: the name of the original image to base the search on
        :param str version: the version of the original image to base the search on
        :param instance image_group: the image group of the original image determined by the
            ``describe_image_group`` method
        :param Iterable rpm_nvrs: the set of binary RPM NVRs that are present or are older than what
            is present in the image
        :param Iterable content_sets: the list of content sets that ``rpm_nvrs`` are in
        :return: a resolved ``ContainerImage`` object representing the fixed published image or
            ``None``
        :rtype: ContainerImage or None
        """
        rpm_name_to_nvrs = {kobo.rpmlib.parse_nvr(nvr)["name"]: nvr for nvr in rpm_nvrs}

        images = self.pyxis.find_latest_images_by_name_version(
            name, version, published=True, content_sets=content_sets
        )
        if not images:
            log.error("Could not find an image with the name and version of %s-%s", name, version)
            return

        images = self.postprocess_images(images, rpm_name_to_nvrs)

        candidate_images = []
        for image in images:
            # If it's not on the same repositories or the regex matched something unexpected, then
            # skip it
            candidate_image_group = self.describe_image_group(image)
            if not image_group.issubset(candidate_image_group):
                log.debug(
                    "The image %s did not have the correct image group (`%s` != `%s`)",
                    image.nvr,
                    candidate_image_group,
                    image_group,
                )
                continue

            # Due to filtering by installed RPMs taking too long in Pyxis, perform the filter
            # here since the projection (returned RPM manifest from Pyxis) has the filtering
            # applied. This is to be conservative in the event a child image relies on the RPM but
            # it is no longer installed
            if {rpm["name"] for rpm in image.get_rpms() or []} != rpm_name_to_nvrs.keys():
                log.debug("The image %s does not contain all the expected RPMs", image.nvr)
                continue

            if not self.filter_out_modularity_mismatch([image], rpm_name_to_nvrs):
                log.debug("The image %s has a modularity mismatch", image.nvr)
                continue

            for rpm in image.get_rpms():
                nvr_in_image = kobo.rpmlib.parse_nvra(rpm["nvra"])
                fixed_nvr = kobo.rpmlib.parse_nvr(rpm_name_to_nvrs[rpm["name"]])
                if kobo.rpmlib.compare_nvr(nvr_in_image, fixed_nvr, ignore_epoch=True) < 0:
                    log.debug("The image %s does not have all the fixed RPMs", image.nvr)
                    break
            else:
                candidate_images.append(image)

        # Remove the images list from memory since this can be quite large
        del images

        if not candidate_images:
            log.debug(
                "No fixed published image was found for the name and version %s-%s", name, version
            )
            return

        # At this point, there is at least one published image with the fixed RPMs and content sets.
        # The next step is to pick the one with the highest release.
        fixed_published_image = candidate_images[0]
        parsed_fixed_published_image_nvr = kobo.rpmlib.parse_nvr(fixed_published_image.nvr)
        for candidate_image in candidate_images[1:]:
            parsed_candidate_image_nvr = kobo.rpmlib.parse_nvr(candidate_image.nvr)
            if (
                kobo.rpmlib.compare_nvr(
                    parsed_candidate_image_nvr, parsed_fixed_published_image_nvr
                )
                > 0
            ):
                fixed_published_image = candidate_image

        # Now that the best fixed published image is determined, get it from pyxis with all the
        # metadata required by Freshmaker
        images = self.pyxis.find_images_by_nvr(fixed_published_image.nvr)
        if not images:
            log.error("The image with the NVR %s was not found in Pyxis", fixed_published_image.nvr)
            return

        images = self.postprocess_images(images, rpm_name_to_nvrs)

        image = images[0]
        image.resolve(self)
        return image
