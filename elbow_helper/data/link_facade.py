"""Compatibility adapter; all ownership selectors live in data.reads."""

from .store.legacy_links import LinkConflict


class LinkFacade:
    def __init__(self, service):
        self.service = service

    def _link_read(self, select=None):
        if self.service is None:
            raise LinkConflict("links_topic_unavailable")
        return self.service.read_links_in_worker(select)

    def _select(self, method, *args):
        # The staged people resolver still imports agent matching helpers.
        # Load selectors only at read time to keep feature imports acyclic.
        from .reads.people_links import LinkSelectors
        return getattr(LinkSelectors, method)(self, *args)

    def all(self):
        return self._select("all")

    def by_tag(self, tag):
        return self._select("by_tag", tag)

    def by_tags(self, tags):
        return self._select("by_tags", tags)

    def for_user(self, user_id):
        return self._select("for_user", user_id)

    def for_members(self, member_ids):
        return self._select("for_members", member_ids)
