import uuid
from typing import Optional, Any
from copy import deepcopy
from collections import deque
from collections.abc import MutableMapping

from .proxies import MapProxy
from .context import Context
from .apply_patch import apply_patch
from .datatypes import Map


class Doc(MutableMapping):
    def __init__(
        self,
        actor_id: Optional[str] = None,
        initial_data: Optional[dict[Any, Any]] = None,
        backend=None,
    ) -> None:
        if actor_id is None:
            # QUESTION: Why do we remove "-"?
            actor_id = str(uuid.uuid4()).replace("-", "")

        # Automerge has a frontend/backend split
        # The backend is the Rust core that implements all the actual CRDT logic
        # The backend can run on a different thread than the frontend
        # It accepts local changes (in JSON/dictionary format)
        # and remote changes in binary format
        # Local changes are made (presumably) on the same device. (For example, a UI edit)
        # remote changes are made by a different user & are compressed into a compact binary
        # format to send over the network
        # This is done so that local changes can be optimistically applied on a UI thread (where latency is important)
        # instead of waiting for a round-trip of change --> backend --> patch before updates can be shown

        self.backend = backend
        if backend:
            # If we pass in a backend, then it is running on the same thread
            # (there's no meaningful frontend/backend split)
            # Normally we get a binary change to send over the network to peers
            # by calling the backend, but if the frontend is calling the backend directly
            # then we need to expose the result to the user
            self.local_bin_changes = []
        else:
            # stores changes generated by the frontend, so the user can pass them to the backend
            self.local_changes = []
            self.in_flight_local_changes = deque([])
            # We only need an optimistic state (to reflect UI edits) if the backend
            # is running on a different thread b/c if the backend is running on the same thread
            # we can just do the "user action --> change --> backend --> patch to apply" synchronously
            self.optimistic_root_obj = None

        self.actor_id = actor_id
        self.ctx = None
        self.seq = 0
        self.max_op = 0

        self.root_obj = Map([], "_root", {})
        if initial_data:
            with self as d:
                for (k, v) in initial_data.items():
                    d[k] = v

    def apply_patch(self, patch):
        if "actorId" in patch and patch["actorId"] == self.actor_id:
            # it's a patch generated from a local change we made
            expected_seq = self.in_flight_local_changes[0]
            if patch["seq"] != expected_seq:
                raise Exception(
                    f"Out of order patch. Expected seq {expected_seq}, received: {patch['seq']}"
                )
            self.in_flight_local_changes.popleft()
            if len(self.in_flight_local_changes) == 0:
                # we've "caught up" (the backed has processed all active local changes and sent back patches for each of them)
                self.optimistic_root_obj = None

        # We're caught up
        if self.optimistic_root_obj == None:
            # TODO: Explain the intuition for why we only
            # use the patch's `maxOp` if we're caught up
            self.max_op = patch["maxOp"]

        # TODO: Why do we look at clock seq instead of using patch["seq"]?
        if self.actor_id in patch["clock"]:
            clock_seq = patch["clock"][self.actor_id]
            if clock_seq > self.seq:
                self.seq = clock_seq

        # we always apply patches to the "true" state.
        # This means remote changes aren't reflected until the optimistic fork is discarded
        self.root_obj = apply_patch(self.root_obj, patch["diffs"])

    def get_recent_ops(self, path):
        temp = self.root_obj
        for segment in path[:-1]:
            temp = temp[segment]
        return temp.recent_ops[path[-1]]

    def get_active_root_obj(self):
        if not self.backend and self.optimistic_root_obj:
            return self.optimistic_root_obj
        return self.root_obj

    def __getitem__(self, key):
        return self.get_active_root_obj()[key]

    def __delitem__(self, key):
        raise Exception(
            f"Cannot delete directly on a document. Use a change block. (Tried deleting {key})"
        )

    def __iter__(self):
        return self.get_active_root_obj().__iter__()

    def __len__(self):
        return self.get_active_root_obj().__len__()

    def __setitem__(self, key, val):
        raise Exception(
            f"Cannot assign directly on a document. Use a change block. (Tried assigning {key} to {val})"
        )

    def __enter__(self):
        active_obj = None
        if self.backend:
            # if we have an integrated backend then there's no optimistic state/fork
            active_obj = self.root_obj
        else:
            # everytime we enter a change block we're generating a local change,
            # when we create a local change, we fork a copy of the state (if it has not already been forked)
            # and apply the local change to the fork (which we present to the user)
            # patches from the backend are applied to the original state

            # we discard the fork once the true state (`self.root_obj`) has "caught up" to the fork
            # (`in_flight_local_changes` is empty)

            # we can't apply patches from the backend to the optimistic fork b/c the backend
            # buffers changes & sends them in an appropriate ordering (the frontend doesn't need to worry about patch order), but the fork breaks this ordering
            # (in order to be optimistic)
            if not self.optimistic_root_obj:
                # create the optimistic state, if it doesn't already exist
                # TODO: Use a mechanism that does cheap copies (like Clojure/immutable.js)
                self.optimistic_root_obj = deepcopy(self.root_obj)
            active_obj = self.optimistic_root_obj
        self.ctx = Context(self.max_op, self.actor_id, active_obj)
        return MapProxy(self.ctx, active_obj, [])

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.seq += 1
        change = {
            "actor": self.actor_id,
            "seq": self.seq,
            "startOp": self.max_op + 1,
            "deps": [],
            "ops": self.ctx.ops,
            # TODO: Use unix timestamp
            "time": 12345,
            "message": "",
        }
        self.max_op = self.max_op + len(self.ctx.ops)
        # TODO: Right now we apply a patch twice in the case of a local change
        # Once, in little chunks in the change context itself (so changes are immediately reflected)
        # And once when the patch reflecting the local change is sent from the backend
        if self.backend:
            patch, change_encoded_as_bin = self.backend.apply_local_change(change)
            self.apply_patch(patch)
            self.local_bin_changes.append(change_encoded_as_bin)
        else:
            self.local_changes.append(change)
            self.in_flight_local_changes.append(change["seq"])
