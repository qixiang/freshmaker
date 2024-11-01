# -*- coding: utf-8 -*-
# Copyright (c) 2022  Red Hat, Inc.
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

from functools import cached_property
from typing import Optional

import backoff
import dogpile.cache
import kobo.rpmlib
from gql import Client, gql
from gql.dsl import DSLQuery, DSLSchema, dsl_gql
from gql.transport.requests import RequestsHTTPTransport

from freshmaker import conf


class PyxisGQLRequestError(Exception):
    """The server returned an error for a specific query"""

    error: str
    trace_id: Optional[str]

    def __init__(self, error: str | list[str], trace_id: Optional[str] = None):
        self.error = str(error)
        self.trace_id = trace_id

        trace_msg = f" trace_id={trace_id}" if trace_id else ""
        msg = str(error) + str(trace_msg)
        super().__init__(msg)


class PyxisGQLRequestTimeout(PyxisGQLRequestError):
    pass


class PyxisGQL:
    region = dogpile.cache.make_region().configure(conf.dogpile_cache_backend, expiration_time=1200)

    def __init__(self, url, cert):
        """Create authenticated Pyxis GraphQL session"""
        transport = RequestsHTTPTransport(url=url, cert=cert, retries=3)

        # Fetch the schema from the transport using an introspection query
        self._client = Client(transport=transport, fetch_schema_from_transport=True)

    @cached_property
    def dsl_schema(self):
        # cached_property can't be cached with dogpile.cache,
        # call _get_graphql_schema as a workaround
        return self._get_graphql_schema()

    @region.cache_on_arguments()
    def _get_graphql_schema(self):
        query = gql(
            """
                query {
                    get_ping
                }
            """
        )
        self._client.execute(query)
        return DSLSchema(self._client.schema)

    @backoff.on_exception(
        backoff.expo,
        PyxisGQLRequestTimeout,
        factor=30,
        max_tries=3,
        jitter=None,  # use deterministic backoff, do not apply random jitter
    )
    def query(self, query_dsl):
        """Execute a GraphQL query with Domain Specific Language

        :params gql.dsl.DSLField query_dsl: a DSL query
        :return: The result of execution.
        """
        response = self._client.execute(dsl_gql(DSLQuery(query_dsl)))

        response_field_name = query_dsl.name
        error = response[response_field_name]["error"]
        if error is not None:
            trace_id = self._client.transport.response_headers.get("trace_id", False)

            if (
                error["detail"]
                and "Pyxis API was unable to fetch data from MongoDB in time" in error["detail"]
            ):
                raise PyxisGQLRequestTimeout(error=error, trace_id=trace_id)
            raise PyxisGQLRequestError(error=error, trace_id=trace_id)

        return response

    def _get_repo_projection(self):
        ds = self.dsl_schema
        projection = [
            ds.ContainerRepository.release_categories,
            ds.ContainerRepository.auto_rebuild_tags,
            ds.ContainerRepository.published,
            ds.ContainerRepository.repository,
        ]
        return projection

    def _get_image_projection(self, include_rpms=True):
        ds = self.dsl_schema
        projection = [
            ds.ContainerImage.architecture,
            ds.ContainerImage.brew.select(
                ds.Brew.build,
                ds.Brew.package,
            ),
            ds.ContainerImage.content_sets,
            ds.ContainerImage.parent_brew_build,
            ds.ContainerImage.parsed_data.select(
                ds.ParsedData.labels.select(
                    ds.Label.name,
                    ds.Label.value,
                ),
            ),
            ds.ContainerImage.repositories.select(
                ds.ContainerImageRepo.registry,
                ds.ContainerImageRepo.repository,
                ds.ContainerImageRepo.published,
                ds.ContainerImageRepo.tags.select(
                    ds.ContainerImageRepoTag.name,
                ),
            ),
        ]

        # Include rpm manifest data in result, use edges to get the rpm manifest
        # data because the direct rpm manifest field doesn't include all data
        if include_rpms:
            projection.append(
                ds.ContainerImage.edges.select(
                    ds.ContainerImageEdges.rpm_manifest.select(
                        ds.ContainerImageRPMManifestResponse.data.select(
                            ds.ContainerImageRPMManifest.image_id,
                            ds.ContainerImageRPMManifest.rpms.select(
                                ds.RpmsItems.name,
                                ds.RpmsItems.nvra,
                                ds.RpmsItems.srpm_name,
                                ds.RpmsItems.srpm_nevra,
                            ),
                        ),
                    ),
                )
            )

        return projection

    @region.cache_on_arguments()
    def find_repositories(
        self,
        published=None,
        release_categories=None,
        auto_rebuild_tags=None,
        names: list[str] | None = None,
    ):
        """Get image repositories

        :param bool published: published or unpublished repositories
        :param list release_categories: list of release categories
        :param list auto_rebuild_tags: list of tags enabled for auto rebuild
        :param names: names of the repos to limit the search to (optional)
        :return: list of image repositories
        :rtype: list
        """
        query_filter: dict = {}
        query_filter["and"] = []
        # Query Red Hat repositories only
        query_filter["and"].append({"vendor_label": {"eq": "redhat"}})

        if isinstance(published, bool):
            query_filter["and"].append({"published": {"eq": published}})

        if release_categories:
            query_filter["and"].append({"release_categories": {"in": release_categories}})

        if auto_rebuild_tags:
            query_filter["and"].append({"auto_rebuild_tags": {"in": auto_rebuild_tags}})
        if names:
            query_filter["and"].append({"repository": {"in": names}})

        repositories = []
        ds = self.dsl_schema

        page_num = 0
        # Iterate all pages
        while True:
            query_dsl = ds.Query.find_repositories(
                page=page_num,
                page_size=conf.pyxis_default_page_size,
                filter=query_filter,
            ).select(
                ds.ContainerRepositoryPaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerRepositoryPaginatedResponse.page,
                ds.ContainerRepositoryPaginatedResponse.page_size,
                ds.ContainerRepositoryPaginatedResponse.total,
                ds.ContainerRepositoryPaginatedResponse.data.select(*self._get_repo_projection()),
            )

            result = self.query(query_dsl)
            data = result["find_repositories"]["data"]
            # Data is empty when there are no more results
            if not data:
                break

            repositories.extend(data)
            # If page_size >= total, means all results have been fetched in the first page
            if result["find_repositories"]["page_size"] >= result["find_repositories"]["total"]:
                break
            page_num += 1

        return repositories

    @region.cache_on_arguments()
    def find_repositories_by_repository_name(self, repository: str) -> list:
        """Get image repositories by repository name

        :param string repository: repository name to filter by
        :return: list of image repositories
        :rtype: list
        """
        query_filter: dict = {}
        query_filter["and"] = []

        query_filter["and"].append({"repository": {"eq": repository}})
        # Query Red Hat repositories only
        query_filter["and"].append({"vendor_label": {"eq": "redhat"}})

        repositories = []
        ds = self.dsl_schema

        page_num = 0
        # Iterate all pages
        while True:
            query_dsl = ds.Query.find_repositories(
                page=page_num,
                page_size=conf.pyxis_default_page_size,
                filter=query_filter,
            ).select(
                ds.ContainerRepositoryPaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerRepositoryPaginatedResponse.page,
                ds.ContainerRepositoryPaginatedResponse.page_size,
                ds.ContainerRepositoryPaginatedResponse.total,
                ds.ContainerRepositoryPaginatedResponse.data.select(*self._get_repo_projection()),
            )

            result = self.query(query_dsl)
            data = result["find_repositories"]["data"]
            # Data is empty when there are no more results
            if not data:
                break

            repositories.extend(data)
            # If page_size >= total, means all results have been fetched in the first page
            if result["find_repositories"]["page_size"] >= result["find_repositories"]["total"]:
                break
            page_num += 1

        return repositories

    @region.cache_on_arguments()
    def find_repositories_by_registry_paths(self, registry_paths):
        """Get image repositories by registry paths

        :param list registry_paths: list of registry paths, each in format of:
            {"registry": registry_name, "repository": repository_name}
        :return: list of image repositories
        :rtype: list
        """
        query_filter = {}
        query_filter["or"] = []

        for path in registry_paths:
            query_filter["or"].append(
                {
                    "and": [
                        {"registry": {"eq": path["registry"]}},
                        {"repository": {"eq": path["repository"]}},
                    ]
                }
            )

        repositories = []
        ds = self.dsl_schema

        page_num = 0
        # Iterate all pages
        while True:
            query_dsl = ds.Query.find_repositories(
                page=page_num,
                page_size=conf.pyxis_default_page_size,
                filter=query_filter,
            ).select(
                ds.ContainerRepositoryPaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerRepositoryPaginatedResponse.page,
                ds.ContainerRepositoryPaginatedResponse.page_size,
                ds.ContainerRepositoryPaginatedResponse.total,
                ds.ContainerRepositoryPaginatedResponse.data.select(*self._get_repo_projection()),
            )

            result = self.query(query_dsl)
            data = result["find_repositories"]["data"]
            # Data is empty when there are no more results
            if not data:
                break

            repositories.extend(data)
            # If page_size >= total, means all results have been fetched in the first page
            if result["find_repositories"]["page_size"] >= result["find_repositories"]["total"]:
                break
            page_num += 1

        return repositories

    @region.cache_on_arguments()
    def get_repository_by_registry_path(self, registry, repository):
        """Get image repository by registry path

        :param str registry: registry name
        :param str repository: repository name
        :return: container repository response
        :rtype: dict
        """
        ds = self.dsl_schema
        query_dsl = ds.Query.get_repository_by_registry_path(
            registry=registry, repository=repository
        ).select(
            ds.ContainerRepositoryResponse.error.select(
                ds.ResponseError.status,
                ds.ResponseError.detail,
            ),
            ds.ContainerRepositoryResponse.data.select(
                *self._get_repo_projection(),
            ),
        )

        result = self.query(query_dsl)
        return result["get_repository_by_registry_path"]["data"]

    @region.cache_on_arguments()
    def find_images_by_nvr(self, nvr: str, include_rpms: bool = True):
        ds = self.dsl_schema

        images = []
        page_num = 0

        # Iterate all pages
        while True:
            query_dsl = ds.Query.find_images_by_nvr(
                page=page_num,
                page_size=conf.pyxis_default_page_size,
                nvr=nvr,
            ).select(
                ds.ContainerImagePaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerImagePaginatedResponse.page,
                ds.ContainerImagePaginatedResponse.page_size,
                ds.ContainerImagePaginatedResponse.total,
                ds.ContainerImagePaginatedResponse.data.select(
                    *self._get_image_projection(include_rpms=include_rpms)
                ),
            )

            result = self.query(query_dsl)
            data = result["find_images_by_nvr"]["data"]
            # Data is empty when there are no more results
            if not data:
                break

            images.extend(data)

            # If page_size >= total, means all results have been fetched in the first page
            if result["find_images_by_nvr"]["page_size"] >= result["find_images_by_nvr"]["total"]:
                break
            page_num += 1

        return images

    @region.cache_on_arguments()
    def find_images_by_nvrs(self, nvrs, include_rpms=True):
        ds = self.dsl_schema

        images = []
        page_num = 0

        # Iterate all pages
        while True:
            query_filter = {"brew": {"build": {"in": nvrs}}}
            query_dsl = ds.Query.find_images(
                page=page_num,
                page_size=conf.pyxis_default_page_size,
                filter=query_filter,
            ).select(
                ds.ContainerImagePaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerImagePaginatedResponse.page,
                ds.ContainerImagePaginatedResponse.page_size,
                ds.ContainerImagePaginatedResponse.total,
                ds.ContainerImagePaginatedResponse.data.select(
                    *self._get_image_projection(include_rpms=include_rpms)
                ),
            )

            result = self.query(query_dsl)
            data = result["find_images"]["data"]
            # Data is empty when there are no more results
            if not data:
                break

            images.extend(data)

            # If page_size >= total, means all results have been fetched in the first page
            if result["find_images"]["page_size"] >= result["find_images"]["total"]:
                break
            page_num += 1

        return images

    def find_images_by_installed_rpms(
        self, rpm_names, content_sets=None, repositories=None, published=None, tags=None
    ):
        """Find images which have the provided rpms installed

        :param list rpm_names: List of rpm names
        :param list content_sets: List of content sets
        :param list repositories: List of repository paths
        :param bool published: The published attribution of image
        :param list tags: List of image tags
        :return: List of image data
        :rtype: list
        """
        images = []

        query_filter = {}
        query_filter["and"] = []

        # List filters from most specific to least specific can help
        # improving the performance of Pyxis query
        repo_matches = []
        if isinstance(published, bool):
            repo_matches.append({"published": {"eq": published}})
        if repositories:
            repo_matches.append({"repository": {"in": repositories}})
        if tags:
            repo_matches.append({"tags_elemMatch": {"and": [{"name": {"in": tags}}]}})
        if repo_matches:
            query_filter["and"].append({"repositories_elemMatch": {"and": repo_matches}})

        if content_sets:
            query_filter["and"].append({"content_sets": {"in": content_sets}})

        query_filter["and"].append({"rpm_manifest": {"rpms": {"name": {"in": rpm_names}}}})

        ds = self.dsl_schema
        page_num = 0

        # This query is resource consuming and Pyxis may not be able to handle it in a time
        # manner when the page_size is large, use the configured small page size to avoid errors.

        # Iterate all pages
        while True:
            query_dsl = ds.Query.find_images(
                page=page_num,
                page_size=conf.pyxis_small_page_size,
                filter=query_filter,
            ).select(
                ds.ContainerImagePaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerImagePaginatedResponse.page,
                ds.ContainerImagePaginatedResponse.page_size,
                ds.ContainerImagePaginatedResponse.data.select(*self._get_image_projection()),
            )

            result = self.query(query_dsl)
            data = result["find_images"]["data"]
            # Data is empty when there are no more results
            if not data:
                break

            # Only keep the rpms we care about, the large rpm manifest data can impact performance
            for img in data:
                rpms = img["edges"]["rpm_manifest"]["data"]["rpms"]
                img["edges"]["rpm_manifest"]["data"]["rpms"] = [
                    rpm for rpm in rpms if rpm["name"] in rpm_names
                ]
            images.extend(data)

            # This page has less items than the page size, it's the last page
            if len(data) < result["find_images"]["page_size"]:
                break
            page_num += 1

        return images

    def find_images_by_names(self, names):
        """Find all the images for a specific list of names.

        :param names list: list of names we want to find images for.
        :return: list of container images matching the requested names.
        :rtype: list of ContainerImages
        """
        images = []
        query_filter = {"and": []}
        query_filter["and"].append({"brew": {"package": {"in": names}}})
        # Only query for published images
        query_filter["and"].append(
            {"repositories_elemMatch": {"and": [{"published": {"eq": True}}]}}
        )

        ds = self.dsl_schema
        page_num = 0

        while True:
            query_dsl = ds.Query.find_images(
                page=page_num,
                page_size=conf.pyxis_default_page_size,
                filter=query_filter,
            ).select(
                ds.ContainerImagePaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerImagePaginatedResponse.page,
                ds.ContainerImagePaginatedResponse.page_size,
                ds.ContainerImagePaginatedResponse.total,
                ds.ContainerImagePaginatedResponse.data.select(
                    *self._get_image_projection(include_rpms=False)
                ),
            )

            result = self.query(query_dsl)
            data = result["find_images"]["data"]
            # Data is empty when there are no more results
            if not data:
                break
            images.extend(data)

            # If page_size >= total, means all results have been fetched in the first page
            if result["find_images"]["page_size"] >= result["find_images"]["total"]:
                break
            page_num += 1

        return images

    def find_images_by_repository(
        self, repository: str, auto_rebuild_tags: Optional[list[str]] = None
    ) -> list:
        """Find images which have the provided repository name and auto_rebuild_tags
        :param string repository: repository name to filter by
        :param list[string] auto_rebuild_tags: repository auto_rebuild_tags to filter by
        :return: List of image data
        :rtype: list
        """
        images = []

        query_filter: dict = {}
        query_filter["and"] = []

        query_filter["and"].append({"repositories": {"repository": {"eq": repository}}})
        query_filter["and"].append({"repositories": {"published": {"eq": True}}})
        if auto_rebuild_tags:
            query_filter["and"].append(
                {"repositories": {"tags": {"name": {"in": auto_rebuild_tags}}}}
            )

        ds = self.dsl_schema
        page_num = 0

        # Iterate all pages
        while True:
            query_dsl = ds.Query.find_images(
                page=page_num,
                page_size=conf.pyxis_default_page_size,
                filter=query_filter,
            ).select(
                ds.ContainerImagePaginatedResponse.error.select(
                    ds.ResponseError.status,
                    ds.ResponseError.detail,
                ),
                ds.ContainerImagePaginatedResponse.page,
                ds.ContainerImagePaginatedResponse.page_size,
                ds.ContainerImagePaginatedResponse.total,
                ds.ContainerImagePaginatedResponse.data.select(
                    *self._get_image_projection(include_rpms=False)
                ),
            )

            result = self.query(query_dsl)
            data = result["find_images"]["data"]
            # Data is empty when there are no more results
            if not data:
                break

            images.extend(data)

            # If page_size >= total, means all results have been fetched in the first page
            if result["find_images"]["page_size"] >= result["find_images"]["total"]:
                break
            page_num += 1

        return images

    def find_latest_images_by_name_version(self, name, version, published=None, content_sets=None):
        """
        Find the latest images that match the specified name, version, and are filtered by the given content sets.

        :param name str: image name
        :param version str: image version
        :param published bool: image is published or not
        :param content_sets list: list of content set names
        :return: list of container images matching the name and version
        :rtype: list of image data
        """
        query_filter = {"and": []}
        query_filter["and"].append({"brew": {"package": {"eq": name}}})
        query_filter["and"].append({"brew": {"build": {"regex": f"{name}-{version}-.*"}}})

        if content_sets:
            query_filter["and"].append({"content_sets": {"in": content_sets}})

        if isinstance(published, bool):
            repo_matches = [{"published": {"eq": published}}]
            query_filter["and"].append({"repositories_elemMatch": {"and": repo_matches}})

        ds = self.dsl_schema
        # Pyxis is having an issue of returning images for such query criteria (ISV-3642),
        # so we use a much smaller page size in this particular query
        page_size = 20

        query_dsl = ds.Query.find_images(
            page=0,
            page_size=page_size,
            # Sort by image NVR (brew.build), newer image should have higher NVR
            sort_by=[{"field": "repositories.published_date", "order": "DESC"}],
            filter=query_filter,
        ).select(
            ds.ContainerImagePaginatedResponse.error.select(
                ds.ResponseError.status,
                ds.ResponseError.detail,
            ),
            ds.ContainerImagePaginatedResponse.page,
            ds.ContainerImagePaginatedResponse.page_size,
            ds.ContainerImagePaginatedResponse.total,
            ds.ContainerImagePaginatedResponse.data.select(*self._get_image_projection()),
        )

        images = self.query(query_dsl)["find_images"]["data"]
        if not images:
            return []

        latest_nvr = images[0]["brew"]["build"]

        for img in images[1:]:
            parsed_latest_nvr = kobo.rpmlib.parse_nvr(latest_nvr)

            current_nvr = img["brew"]["build"]
            parsed_current_nvr = kobo.rpmlib.parse_nvr(current_nvr)
            if kobo.rpmlib.compare_nvr(parsed_current_nvr, parsed_latest_nvr) == 1:
                latest_nvr = current_nvr

        return [img for img in images if img["brew"]["build"] == latest_nvr]
