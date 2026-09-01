from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from elbow_helper.features.role_connections.builder import ConditionBuilderView
from elbow_helper.features.role_connections.config import INVALID_DEPENDENCY_MESSAGE
from elbow_helper.features.role_connections.cog import RoleConnections
from elbow_helper.features.role_connections.edit import RoleListAddSelect
from elbow_helper.features.role_connections.edit import TargetRoleEditSelect


def _connection(
    connection_id: str,
    target_role_id: int,
    *,
    has: tuple[int, ...] = (),
    missing: tuple[int, ...] = (),
) -> dict[str, object]:
    return {
        "id": connection_id,
        "target_role_id": target_role_id,
        "all": [
            *({"has": role_id} for role_id in has),
            *({"not": role_id} for role_id in missing),
        ],
        "any": [],
    }


class RoleConnectionValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_direct_self_reference_is_rejected(self) -> None:
        cog = object.__new__(RoleConnections)
        cog.state = {"connections": []}

        self.assertFalse(
            cog.connection_change_is_valid(
                _connection("self", 10, missing=(10,)),
            )
        )

    def test_indirect_dependency_cycle_is_rejected(self) -> None:
        cog = object.__new__(RoleConnections)
        cog.state = {
            "connections": [
                _connection("role-a", 10, has=(20,)),
            ]
        }

        self.assertFalse(
            cog.connection_change_is_valid(
                _connection("role-b", 20, has=(10,)),
            )
        )

    def test_one_way_dependency_is_accepted(self) -> None:
        cog = object.__new__(RoleConnections)
        cog.state = {
            "connections": [
                _connection("role-a", 10, has=(20,)),
            ]
        }

        self.assertTrue(
            cog.connection_change_is_valid(
                _connection("role-b", 30, has=(10,)),
            )
        )

    def test_edit_that_completes_dependency_cycle_is_rejected(self) -> None:
        cog = object.__new__(RoleConnections)
        cog.state = {
            "connections": [
                _connection("role-a", 10, has=(20,)),
                _connection("role-b", 20, has=(30,)),
            ]
        }

        self.assertFalse(
            cog.connection_change_is_valid(
                _connection("role-b", 20, has=(10,)),
                replacing_id="role-b",
            )
        )

    async def test_existing_cyclic_rule_is_not_applied(self) -> None:
        role = SimpleNamespace(id=10)
        member = SimpleNamespace(
            roles=[],
            guild=SimpleNamespace(get_role=MagicMock(return_value=role)),
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        cog = object.__new__(RoleConnections)
        cog.state = {
            "connections": [
                _connection("self", 10, missing=(10,)),
            ]
        }

        with self.assertLogs(
            "elbow_helper.features.role_connections.cog",
            level="ERROR",
        ):
            added, removed = await cog._apply_connections_to_member(
                member,
                reason="test",
            )

        self.assertEqual((added, removed), (0, 0))
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    async def test_new_cyclic_connection_is_not_saved(self) -> None:
        cog = SimpleNamespace(
            state={"connections": []},
            new_connection_id=MagicMock(return_value="self"),
            connection_change_is_valid=MagicMock(return_value=False),
        )
        view = SimpleNamespace(
            cog=cog,
            target_role_id=10,
            conditions_all=[{"not": 10}],
            conditions_any=[],
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await ConditionBuilderView.finish(view, interaction)

        self.assertEqual(cog.state["connections"], [])
        interaction.response.send_message.assert_awaited_once_with(
            INVALID_DEPENDENCY_MESSAGE,
            ephemeral=True,
        )

    async def test_cyclic_target_edit_is_not_saved(self) -> None:
        connection = _connection("role-a", 10, has=(20,))
        cog = SimpleNamespace(
            get_connection=MagicMock(return_value=connection),
            connection_change_is_valid=MagicMock(return_value=False),
            update_connection_target=MagicMock(),
        )
        parent_view = SimpleNamespace(cog=cog, conn_id="role-a")
        select = SimpleNamespace(
            _parent_view=parent_view,
            values=[SimpleNamespace(id=20)],
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await TargetRoleEditSelect.callback(select, interaction)

        cog.update_connection_target.assert_not_called()
        interaction.response.send_message.assert_awaited_once_with(
            INVALID_DEPENDENCY_MESSAGE,
            ephemeral=True,
        )

    async def test_cyclic_condition_edit_is_not_saved(self) -> None:
        connection = _connection("role-a", 10)
        cog = SimpleNamespace(
            get_connection=MagicMock(return_value=connection),
            connection_change_is_valid=MagicMock(return_value=False),
            add_connection_roles=MagicMock(),
        )
        parent_view = SimpleNamespace(
            cog=cog,
            conn_id="role-a",
            list_name="all",
            kind="not",
        )
        select = SimpleNamespace(
            _parent_view=parent_view,
            values=[SimpleNamespace(id=10)],
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await RoleListAddSelect.callback(select, interaction)

        cog.add_connection_roles.assert_not_called()
        interaction.response.send_message.assert_awaited_once_with(
            INVALID_DEPENDENCY_MESSAGE,
            ephemeral=True,
        )


if __name__ == "__main__":
    unittest.main()
