from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from elbow_helper.features.role_connections.builder import ConditionBuilderView
from elbow_helper.features.role_connections.config import INVALID_DEPENDENCY_MESSAGE
from elbow_helper.features.role_connections.config import PERSISTENCE_ERROR_MESSAGE
from elbow_helper.features.role_connections.cog import RoleConnections
from elbow_helper.features.role_connections.edit import RemoveConnectionSelect
from elbow_helper.features.role_connections.edit import RoleListAddSelect
from elbow_helper.features.role_connections.edit import RoleListRemoveSelect
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


class RoleConnectionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def test_failed_writes_leave_live_state_unchanged(self) -> None:
        operations = {
            "add connection": lambda cog: cog.add_connection(
                _connection("role-b", 20)
            ),
            "remove connection": lambda cog: cog.remove_connection("role-a"),
            "change target": lambda cog: cog.update_connection_target(
                "role-a",
                20,
            ),
            "add condition": lambda cog: cog.add_connection_roles(
                "role-a",
                "all",
                "has",
                [20],
            ),
            "remove condition": lambda cog: cog.remove_connection_roles(
                "role-a",
                "all",
                "has",
                [30],
            ),
        }

        for name, operation in operations.items():
            with self.subTest(operation=name):
                cog = object.__new__(RoleConnections)
                cog.state = {
                    "connections": [
                        _connection("role-a", 10, has=(30,)),
                    ]
                }
                original_state = cog.state
                original_connections = list(cog.state["connections"])

                with (
                    patch(
                        "elbow_helper.features.role_connections.cog.save_state",
                        side_effect=OSError("disk unavailable"),
                    ),
                    self.assertRaises(OSError),
                ):
                    operation(cog)

                self.assertIs(cog.state, original_state)
                self.assertEqual(
                    cog.state["connections"],
                    original_connections,
                )

    def test_successful_write_replaces_live_state_after_saving(self) -> None:
        cog = object.__new__(RoleConnections)
        cog.state = {"connections": []}

        with patch(
            "elbow_helper.features.role_connections.cog.save_state",
        ) as save:
            cog.add_connection(_connection("role-a", 10))

        saved_state = save.call_args.args[0]
        self.assertIs(cog.state, saved_state)
        self.assertEqual(
            cog.state["connections"],
            [_connection("role-a", 10)],
        )

    async def test_every_mutation_reports_save_failure(self) -> None:
        cases = []

        add_connection = MagicMock(side_effect=OSError("disk unavailable"))
        cases.append(
            (
                "add connection",
                ConditionBuilderView.finish,
                SimpleNamespace(
                    cog=SimpleNamespace(
                        new_connection_id=MagicMock(return_value="role-a"),
                        connection_change_is_valid=MagicMock(return_value=True),
                        add_connection=add_connection,
                    ),
                    target_role_id=10,
                    conditions_all=[{"has": 20}],
                    conditions_any=[],
                ),
                add_connection,
            )
        )

        remove_connection = MagicMock(side_effect=OSError("disk unavailable"))
        cases.append(
            (
                "remove connection",
                RemoveConnectionSelect.callback,
                SimpleNamespace(
                    values=["role-a"],
                    _parent_view=SimpleNamespace(
                        cog=SimpleNamespace(remove_connection=remove_connection),
                    ),
                ),
                remove_connection,
            )
        )

        update_target = MagicMock(side_effect=OSError("disk unavailable"))
        cases.append(
            (
                "change target",
                TargetRoleEditSelect.callback,
                SimpleNamespace(
                    values=[SimpleNamespace(id=20)],
                    _parent_view=SimpleNamespace(
                        cog=SimpleNamespace(
                            get_connection=MagicMock(
                                return_value=_connection("role-a", 10)
                            ),
                            connection_change_is_valid=MagicMock(return_value=True),
                            update_connection_target=update_target,
                        ),
                        conn_id="role-a",
                    ),
                ),
                update_target,
            )
        )

        add_roles = MagicMock(side_effect=OSError("disk unavailable"))
        cases.append(
            (
                "add condition",
                RoleListAddSelect.callback,
                SimpleNamespace(
                    values=[SimpleNamespace(id=20)],
                    _parent_view=SimpleNamespace(
                        cog=SimpleNamespace(
                            get_connection=MagicMock(
                                return_value=_connection("role-a", 10)
                            ),
                            connection_change_is_valid=MagicMock(return_value=True),
                            add_connection_roles=add_roles,
                        ),
                        conn_id="role-a",
                        list_name="all",
                        kind="has",
                    ),
                ),
                add_roles,
            )
        )

        remove_roles = MagicMock(side_effect=OSError("disk unavailable"))
        cases.append(
            (
                "remove condition",
                RoleListRemoveSelect.callback,
                SimpleNamespace(
                    values=["20"],
                    _parent_view=SimpleNamespace(
                        cog=SimpleNamespace(remove_connection_roles=remove_roles),
                        conn_id="role-a",
                        list_name="all",
                        kind="has",
                    ),
                ),
                remove_roles,
            )
        )

        for name, callback, target, operation in cases:
            with self.subTest(operation=name):
                interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        send_message=AsyncMock(),
                        edit_message=AsyncMock(),
                    ),
                )

                await callback(target, interaction)

                operation.assert_called_once()
                interaction.response.send_message.assert_awaited_once_with(
                    PERSISTENCE_ERROR_MESSAGE,
                    ephemeral=True,
                )
                interaction.response.edit_message.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
