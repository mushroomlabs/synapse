import logging
from enum import Enum
from typing import TYPE_CHECKING, AbstractSet, Dict, Final, List, Optional, Set, Tuple

import attr

from synapse._pydantic_compat import HAS_PYDANTIC_V2

if TYPE_CHECKING or HAS_PYDANTIC_V2:
    from pydantic.v1 import Extra
else:
    from pydantic import Extra

from synapse.api.constants import AccountDataTypes, EventTypes, Membership
from synapse.events import EventBase
from synapse.rest.client.models import SlidingSyncBody
from synapse.types import JsonMapping, Requester, RoomStreamToken, StreamToken, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# Everything except `Membership.LEAVE`
MEMBERSHIP_TO_DISPLAY_IN_SYNC = (
    Membership.INVITE,
    Membership.JOIN,
    Membership.KNOCK,
    Membership.BAN,
)


class SlidingSyncConfig(SlidingSyncBody):
    """
    Inherit from `SlidingSyncBody` since we need all of the same fields and add a few
    extra fields that we need in the handler
    """

    user: UserID
    device_id: Optional[str]

    # Pydantic config
    class Config:
        # By default, ignore fields that we don't recognise.
        extra = Extra.ignore
        # By default, don't allow fields to be reassigned after parsing.
        allow_mutation = False
        # Allow custom types like `UserID` to be used in the model
        arbitrary_types_allowed = True


class OperationType(Enum):
    """
    Represents the operation types in a Sliding Sync window.

    Attributes:
        SYNC: Sets a range of entries. Clients SHOULD discard what they previous knew about
            entries in this range.
        INSERT: Sets a single entry. If the position is not empty then clients MUST move
            entries to the left or the right depending on where the closest empty space is.
        DELETE: Remove a single entry. Often comes before an INSERT to allow entries to move
            places.
        INVALIDATE: Remove a range of entries. Clients MAY persist the invalidated range for
            offline support, but they should be treated as empty when additional operations
            which concern indexes in the range arrive from the server.
    """

    SYNC: Final = "SYNC"
    INSERT: Final = "INSERT"
    DELETE: Final = "DELETE"
    INVALIDATE: Final = "INVALIDATE"


@attr.s(slots=True, frozen=True, auto_attribs=True)
class SlidingSyncResult:
    """
    The Sliding Sync result to be serialized to JSON for a response.

    Attributes:
        next_pos: The next position token in the sliding window to request (next_batch).
        lists: Sliding window API. A map of list key to list results.
        rooms: Room subscription API. A map of room ID to room subscription to room results.
        extensions: Extensions API. A map of extension key to extension results.
    """

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class RoomResult:
        """
        Attributes:
            name: Room name or calculated room name.
            avatar: Room avatar
            heroes: List of stripped membership events (containing `user_id` and optionally
                `avatar_url` and `displayname`) for the users used to calculate the room name.
            initial: Flag which is set when this is the first time the server is sending this
                data on this connection. Clients can use this flag to replace or update
                their local state. When there is an update, servers MUST omit this flag
                entirely and NOT send "initial":false as this is wasteful on bandwidth. The
                absence of this flag means 'false'.
            required_state: The current state of the room
            timeline: Latest events in the room. The last event is the most recent
            is_dm: Flag to specify whether the room is a direct-message room (most likely
                between two people).
            invite_state: Stripped state events. Same as `rooms.invite.$room_id.invite_state`
                in sync v2, absent on joined/left rooms
            prev_batch: A token that can be passed as a start parameter to the
                `/rooms/<room_id>/messages` API to retrieve earlier messages.
            limited: True if their are more events than fit between the given position and now.
                Sync again to get more.
            joined_count: The number of users with membership of join, including the client's
                own user ID. (same as sync `v2 m.joined_member_count`)
            invited_count: The number of users with membership of invite. (same as sync v2
                `m.invited_member_count`)
            notification_count: The total number of unread notifications for this room. (same
                as sync v2)
            highlight_count: The number of unread notifications for this room with the highlight
                flag set. (same as sync v2)
            num_live: The number of timeline events which have just occurred and are not historical.
                The last N events are 'live' and should be treated as such. This is mostly
                useful to determine whether a given @mention event should make a noise or not.
                Clients cannot rely solely on the absence of `initial: true` to determine live
                events because if a room not in the sliding window bumps into the window because
                of an @mention it will have `initial: true` yet contain a single live event
                (with potentially other old events in the timeline).
        """

        name: str
        avatar: Optional[str]
        heroes: Optional[List[EventBase]]
        initial: bool
        required_state: List[EventBase]
        timeline: List[EventBase]
        is_dm: bool
        invite_state: List[EventBase]
        prev_batch: StreamToken
        limited: bool
        joined_count: int
        invited_count: int
        notification_count: int
        highlight_count: int
        num_live: int

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class SlidingWindowList:
        """
        Attributes:
            count: The total number of entries in the list. Always present if this list
                is.
            ops: The sliding list operations to perform.
        """

        @attr.s(slots=True, frozen=True, auto_attribs=True)
        class Operation:
            """
            Attributes:
                op: The operation type to perform.
                range: Which index positions are affected by this operation. These are
                    both inclusive.
                room_ids: Which room IDs are affected by this operation. These IDs match
                    up to the positions in the `range`, so the last room ID in this list
                    matches the 9th index. The room data is held in a separate object.
            """

            op: OperationType
            range: Tuple[int, int]
            room_ids: List[str]

        count: int
        ops: List[Operation]

    next_pos: StreamToken
    lists: Dict[str, SlidingWindowList]
    rooms: List[RoomResult]
    extensions: JsonMapping

    def __bool__(self) -> bool:
        """Make the result appear empty if there are no updates. This is used
        to tell if the notifier needs to wait for more events when polling for
        events.
        """
        return bool(self.lists or self.rooms or self.extensions)


class SlidingSyncHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs_config = hs.config
        self.rooms_to_exclude_globally = hs.config.server.rooms_to_exclude_from_sync
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.auth_blocking = hs.get_auth_blocking()
        self.notifier = hs.get_notifier()
        self.event_sources = hs.get_event_sources()
        self.room_summary_handler = hs.get_room_summary_handler()

    async def wait_for_sync_for_user(
        self,
        requester: Requester,
        sync_config: SlidingSyncConfig,
        from_token: Optional[StreamToken] = None,
        timeout: int = 0,
    ) -> SlidingSyncResult:
        """Get the sync for a client if we have new data for it now. Otherwise
        wait for new data to arrive on the server. If the timeout expires, then
        return an empty sync result.
        """
        # If the user is not part of the mau group, then check that limits have
        # not been exceeded (if not part of the group by this point, almost certain
        # auth_blocking will occur)
        await self.auth_blocking.check_auth_blocking(requester=requester)

        # TODO: If the To-Device extension is enabled and we have a `from_token`, delete
        # any to-device messages before that token (since we now know that the device
        # has received them). (see sync v2 for how to do this)

        if timeout == 0 or from_token is None:
            now_token = self.event_sources.get_current_token()
            result = await self.current_sync_for_user(
                sync_config,
                from_token=from_token,
                to_token=now_token,
            )
        else:
            # Otherwise, we wait for something to happen and report it to the user.
            async def current_sync_callback(
                before_token: StreamToken, after_token: StreamToken
            ) -> SlidingSyncResult:
                return await self.current_sync_for_user(
                    sync_config,
                    from_token=from_token,
                    to_token=after_token,
                )

            result = await self.notifier.wait_for_events(
                sync_config.user.to_string(),
                timeout,
                current_sync_callback,
                from_token=from_token,
            )

        return result

    async def current_sync_for_user(
        self,
        sync_config: SlidingSyncConfig,
        to_token: StreamToken,
        from_token: Optional[StreamToken] = None,
    ) -> SlidingSyncResult:
        """
        Generates the response body of a Sliding Sync result, represented as a
        `SlidingSyncResult`.
        """
        user_id = sync_config.user.to_string()
        app_service = self.store.get_app_service_by_user_id(user_id)
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()

        # Get all of the room IDs that the user should be able to see in the sync
        # response
        room_id_set = await self.get_sync_room_ids_for_user(
            sync_config.user,
            from_token=from_token,
            to_token=to_token,
        )

        # Assemble sliding window lists
        lists: Dict[str, SlidingSyncResult.SlidingWindowList] = {}
        if sync_config.lists:
            for list_key, list_config in sync_config.lists.items():
                # Apply filters
                filtered_room_ids = room_id_set
                if list_config.filters is not None:
                    filtered_room_ids = await self.filter_rooms(
                        sync_config.user, room_id_set, list_config.filters
                    )
                # TODO: Apply sorts
                sorted_room_ids = sorted(filtered_room_ids)

                ops: List[SlidingSyncResult.SlidingWindowList.Operation] = []
                if list_config.ranges:
                    for range in list_config.ranges:
                        ops.append(
                            SlidingSyncResult.SlidingWindowList.Operation(
                                op=OperationType.SYNC,
                                range=range,
                                room_ids=sorted_room_ids[range[0] : range[1]],
                            )
                        )

                lists[list_key] = SlidingSyncResult.SlidingWindowList(
                    count=len(sorted_room_ids),
                    ops=ops,
                )

        return SlidingSyncResult(
            next_pos=to_token,
            lists=lists,
            # TODO: Gather room data for rooms in lists and `sync_config.room_subscriptions`
            rooms=[],
            extensions={},
        )

    async def get_sync_room_ids_for_user(
        self,
        user: UserID,
        to_token: StreamToken,
        from_token: Optional[StreamToken] = None,
    ) -> AbstractSet[str]:
        """
        Fetch room IDs that should be listed for this user in the sync response.

        We're looking for rooms that the user has not left (`invite`, `knock`, `join`,
        and `ban`) or newly_left rooms that are > `from_token` and <= `to_token`.
        """
        user_id = user.to_string()

        # First grab a current snapshot rooms for the user
        room_for_user_list = await self.store.get_rooms_for_local_user_where_membership_is(
            user_id=user_id,
            # We want to fetch any kind of membership (joined and left rooms) in order
            # to get the `stream_ordering` of the latest room membership event for the
            # user.
            #
            # We will filter out the rooms that the user has left below (see
            # `MEMBERSHIP_TO_DISPLAY_IN_SYNC`)
            membership_list=Membership.LIST,
            excluded_rooms=self.rooms_to_exclude_globally,
        )

        # If the user has never joined any rooms before, we can just return an empty list
        if not room_for_user_list:
            return set()

        # Our working list of rooms that can show up in the sync response
        sync_room_id_set = {
            room_for_user.room_id
            for room_for_user in room_for_user_list
            if room_for_user.membership in MEMBERSHIP_TO_DISPLAY_IN_SYNC
        }

        # Find the stream_ordering of the latest room membership event which will mark
        # the spot we queried up to.
        max_stream_ordering_from_room_list = max(
            room_for_user.stream_ordering for room_for_user in room_for_user_list
        )

        # If our `to_token` is already the same or ahead of the latest room membership
        # for the user, we can just straight-up return the room list (nothing has
        # changed)
        if max_stream_ordering_from_room_list <= to_token.room_key.stream:
            return sync_room_id_set

        # We assume the `from_token` is before or at-least equal to the `to_token`
        assert (
            from_token is None or from_token.room_key.stream <= to_token.room_key.stream
        ), f"{from_token.room_key.stream if from_token else None} <= {to_token.room_key.stream}"

        # We assume the `from_token`/`to_token` is before the `max_stream_ordering_from_room_list`
        assert (
            from_token is None
            or from_token.room_key.stream < max_stream_ordering_from_room_list
        ), f"{from_token.room_key.stream if from_token else None} < {max_stream_ordering_from_room_list}"
        assert (
            to_token.room_key.stream < max_stream_ordering_from_room_list
        ), f"{to_token.room_key.stream} < {max_stream_ordering_from_room_list}"

        # Since we fetched the users room list at some point in time after the from/to
        # tokens, we need to revert/rewind some membership changes to match the point in
        # time of the `to_token`.
        #
        # - 1) Add back newly_left rooms (> `from_token` and <= `to_token`)
        # - 2a) Remove rooms that the user joined after the `to_token`
        # - 2b) Add back rooms that the user left after the `to_token`
        membership_change_events = await self.store.get_membership_changes_for_user(
            user_id,
            # Start from the `from_token` if given, otherwise from the `to_token` so we
            # can still do the 2) fixups.
            from_key=from_token.room_key if from_token else to_token.room_key,
            # Fetch up to our membership snapshot
            to_key=RoomStreamToken(stream=max_stream_ordering_from_room_list),
            excluded_rooms=self.rooms_to_exclude_globally,
        )

        # Assemble a list of the last membership events in some given ranges. Someone
        # could have left and joined multiple times during the given range but we only
        # care about end-result so we grab the last one.
        last_membership_change_by_room_id_in_from_to_range: Dict[str, EventBase] = {}
        last_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        # We also need the first membership event after the `to_token` so we can step
        # backward to the previous membership that would apply to the from/to range.
        first_membership_change_by_room_id_after_to_token: Dict[str, EventBase] = {}
        for event in membership_change_events:
            assert event.internal_metadata.stream_ordering

            if (
                (
                    from_token is None
                    or event.internal_metadata.stream_ordering
                    > from_token.room_key.stream
                )
                and event.internal_metadata.stream_ordering <= to_token.room_key.stream
            ):
                last_membership_change_by_room_id_in_from_to_range[event.room_id] = (
                    event
                )
            elif (
                event.internal_metadata.stream_ordering > to_token.room_key.stream
                and event.internal_metadata.stream_ordering
                <= max_stream_ordering_from_room_list
            ):
                last_membership_change_by_room_id_after_to_token[event.room_id] = event
                # Only set if we haven't already set it
                first_membership_change_by_room_id_after_to_token.setdefault(
                    event.room_id, event
                )
            else:
                # We don't expect this to happen since we should only be fetching
                # `membership_change_events` that fall in the given ranges above. It
                # doesn't hurt anything to ignore an event we don't need but may
                # indicate a bug in the logic above.
                raise AssertionError(
                    "Membership event with stream_ordering=%s should fall in the given ranges above"
                    + " (%d > x <= %d) or (%d > x <= %d). We shouldn't be fetching extra membership"
                    + " events that aren't used.",
                    event.internal_metadata.stream_ordering,
                    from_token.room_key.stream if from_token else None,
                    to_token.room_key.stream,
                    to_token.room_key.stream,
                    max_stream_ordering_from_room_list,
                )

        # 1)
        for (
            last_membership_change_in_from_to_range
        ) in last_membership_change_by_room_id_in_from_to_range.values():
            room_id = last_membership_change_in_from_to_range.room_id

            # 1) Add back newly_left rooms (> `from_token` and <= `to_token`). We
            # include newly_left rooms because the last event that the user should see
            # is their own leave event
            if last_membership_change_in_from_to_range.membership == Membership.LEAVE:
                sync_room_id_set.add(room_id)

        # 2)
        for (
            last_membership_change_after_to_token
        ) in last_membership_change_by_room_id_after_to_token.values():
            room_id = last_membership_change_after_to_token.room_id

            # We want to find the first membership change after the `to_token` then step
            # backward to know the membership in the from/to range.
            first_membership_change_after_to_token = (
                first_membership_change_by_room_id_after_to_token.get(room_id)
            )
            assert first_membership_change_after_to_token is not None, (
                "If there was a `last_membership_change_after_to_token` that we're iterating over, "
                + "then there should be corresponding a first change. For example, even if there "
                + "is only one event after the `to_token`, the first and last event will be same event. "
                + "This is probably a mistake in assembling the `last_membership_change_by_room_id_after_to_token`"
                + "/`first_membership_change_by_room_id_after_to_token` dicts above."
            )
            prev_content = first_membership_change_after_to_token.unsigned.get(
                "prev_content", {}
            )
            prev_membership = prev_content.get("membership", None)

            # 2a) Add back rooms that the user left after the `to_token`
            #
            # If the last membership event after the `to_token` is a leave event, then
            # the room was excluded from the
            # `get_rooms_for_local_user_where_membership_is()` results. We should add
            # these rooms back as long as the user was part of the room before the
            # `to_token`.
            if (
                last_membership_change_after_to_token.membership == Membership.LEAVE
                and prev_membership is not None
                and prev_membership != Membership.LEAVE
            ):
                sync_room_id_set.add(room_id)
            # 2b) Remove rooms that the user joined (hasn't left) after the `to_token`
            #
            # If the last membership event after the `to_token` is a "join" event, then
            # the room was included in the `get_rooms_for_local_user_where_membership_is()`
            # results. We should remove these rooms as long as the user wasn't part of
            # the room before the `to_token`.
            elif (
                last_membership_change_after_to_token.membership != Membership.LEAVE
                and (prev_membership is None or prev_membership == Membership.LEAVE)
            ):
                sync_room_id_set.discard(room_id)

        return sync_room_id_set

    async def filter_rooms(
        self,
        user: UserID,
        room_id_set: AbstractSet[str],
        filters: SlidingSyncConfig.SlidingSyncList.Filters,
    ) -> AbstractSet[str]:
        """
        Filter rooms based on the sync request.
        """
        user_id = user.to_string()

        # TODO: Re-order filters so that the easiest, most likely to eliminate rooms,
        # are first. This way when people use multiple filters, we can eliminate rooms
        # and do less work for the subsequent filters.
        #
        # TODO: Exclude partially stated rooms unless the `required_state` has
        # `["m.room.member", "$LAZY"]`

        filtered_room_id_set = set(room_id_set)

        # Filter for Direct-Message (DM) rooms
        if filters.is_dm is not None:
            # We're using global account data (`m.direct`) instead of checking for
            # `is_direct` on membership events because that property only appears for
            # the invitee membership event (doesn't show up for the inviter). Account
            # data is set by the client so it needs to be scrutinized.
            dm_map = await self.store.get_global_account_data_by_type_for_user(
                user_id, AccountDataTypes.DIRECT
            )
            logger.warn("dm_map: %s", dm_map)
            # Flatten out the map
            dm_room_id_set = set()
            if dm_map:
                for room_ids in dm_map.values():
                    # Account data should be a list of room IDs. Ignore anything else
                    if isinstance(room_ids, list):
                        for room_id in room_ids:
                            if isinstance(room_id, str):
                                dm_room_id_set.add(room_id)

            if filters.is_dm:
                # Only DM rooms please
                filtered_room_id_set = filtered_room_id_set.intersection(dm_room_id_set)
            else:
                # Only non-DM rooms please
                filtered_room_id_set = filtered_room_id_set.difference(dm_room_id_set)

        # Filter the room based on the space they belong to according to `m.space.child`
        # state events. If multiple spaces are present, a room can be part of any one of
        # the listed spaces (OR'd).
        if filters.spaces:
            # Only use spaces that we're joined to to avoid leaking private space
            # information that the user is not part of. We could probably allow
            # public spaces here but the spec says "joined" only.
            joined_space_room_ids = set()
            for space_room_id in set(filters.spaces):
                # TODO: Is there a good method to look up all space rooms at once? (N+1 query problem)
                is_user_in_room = await self.store.check_local_user_in_room(
                    user_id=user.to_string(), room_id=space_room_id
                )

                if is_user_in_room:
                    joined_space_room_ids.add(space_room_id)

            # Flatten the child rooms in the spaces
            space_child_room_ids: Set[str] = set()
            for space_room_id in joined_space_room_ids:
                space_child_events = (
                    await self.room_summary_handler._get_space_child_events(
                        space_room_id
                    )
                )
                space_child_room_ids.update(
                    event.state_key for event in space_child_events
                )
                # TODO: The spec says that if the child room has a `m.room.tombstone`
                # event, we should recursively navigate until we find the latest room
                # and include those IDs (although this point is under scrutiny).

            # Only rooms in the spaces please
            filtered_room_id_set = filtered_room_id_set.intersection(
                space_child_room_ids
            )

        # Filter for encrypted rooms
        if filters.is_encrypted is not None:
            # Make a copy so we don't run into an error: `Set changed size during iteration`
            for room_id in list(filtered_room_id_set):
                # TODO: Is there a good method to look up all rooms at once? (N+1 query problem)
                is_encrypted = (
                    await self.storage_controllers.state.get_current_state_event(
                        room_id, EventTypes.RoomEncryption, ""
                    )
                )

                # If we're looking for encrypted rooms, filter out rooms that are not
                # encrypted and vice versa
                if (filters.is_encrypted and not is_encrypted) or (
                    not filters.is_encrypted and is_encrypted
                ):
                    filtered_room_id_set.remove(room_id)

        if filters.is_invite:
            raise NotImplementedError()

        if filters.room_types:
            raise NotImplementedError()

        if filters.not_room_types:
            raise NotImplementedError()

        if filters.room_name_like:
            raise NotImplementedError()

        if filters.tags:
            raise NotImplementedError()

        if filters.not_tags:
            raise NotImplementedError()

        return filtered_room_id_set
