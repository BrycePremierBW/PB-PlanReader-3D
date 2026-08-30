import sys
import unittest
from unittest.mock import MagicMock, patch
import pb_planreader_3d_app as app


class TestAuthSecurityV161(unittest.TestCase):

    def setUp(self):
        self.login_screen_mock = MagicMock()
        self.sidebar_ws_mock = MagicMock(return_value=None)
        self.get_bridge_mock = MagicMock(return_value=None)
        self.init_db_mock = MagicMock()

    @patch("streamlit.set_page_config")
    @patch("streamlit.sidebar")
    @patch("streamlit.query_params", {})
    def test_unauthenticated_reset_query_param_does_not_manufacture_user(self, mock_sidebar, mock_set_page_config):
        """A. Unauthenticated session with reset=1 or reset=true cannot obtain planreader_user."""
        for param_val in ["1", "true", "yes", "anything"]:
            with patch.object(app.st, "session_state", {}), \
                 patch.object(app.st, "query_params", {"reset": param_val}), \
                 patch.object(app, "login_screen", self.login_screen_mock), \
                 patch.object(app, "sidebar_workspace_selector", self.sidebar_ws_mock), \
                 patch.object(app, "get_jobhub_bridge", self.get_bridge_mock), \
                 patch.object(app, "init_local_db", self.init_db_mock), \
                 patch.object(app, "app_css", MagicMock()):
                
                self.login_screen_mock.reset_mock()
                app.main()
                
                # Check user is still None
                self.assertIsNone(app.st.session_state.get("planreader_user"))
                # Check login_screen was called
                self.login_screen_mock.assert_called_once()

    @patch("streamlit.set_page_config")
    @patch("streamlit.sidebar")
    def test_query_params_cannot_force_workspace_id_before_auth(self, mock_sidebar, mock_set_page_config):
        """B. Query params cannot force workspace_id before authentication."""
        session_state = {}
        query_params = {"workspace_id": "1", "reset": "1", "ws": "99"}
        
        with patch.object(app.st, "session_state", session_state), \
             patch.object(app.st, "query_params", query_params), \
             patch.object(app, "login_screen", self.login_screen_mock), \
             patch.object(app, "sidebar_workspace_selector", self.sidebar_ws_mock), \
             patch.object(app, "get_jobhub_bridge", self.get_bridge_mock), \
             patch.object(app, "init_local_db", self.init_db_mock), \
             patch.object(app, "app_css", MagicMock()):
            
            app.main()
            
            self.assertIsNone(session_state.get("planreader_user"))
            self.assertNotIn("workspace_id", session_state)
            self.login_screen_mock.assert_called_once()

    @patch("streamlit.set_page_config")
    @patch("streamlit.sidebar")
    def test_existing_legitimate_session_not_replaced(self, mock_sidebar, mock_set_page_config):
        """C. Existing legitimate authenticated session is not replaced with a fabricated role/user."""
        legit_user = {"username": "alice_senior", "role": "Senior Estimator"}
        session_state = {"planreader_user": legit_user, "workspace_id": 42}
        query_params = {"reset": "1"}
        
        with patch.object(app.st, "session_state", session_state), \
             patch.object(app.st, "query_params", query_params), \
             patch.object(app, "login_screen", self.login_screen_mock), \
             patch.object(app, "sidebar_workspace_selector", self.sidebar_ws_mock), \
             patch.object(app, "get_jobhub_bridge", self.get_bridge_mock), \
             patch.object(app, "init_local_db", self.init_db_mock), \
             patch.object(app, "app_css", MagicMock()):
            
            app.main()
            
            # Assert legitimate user was preserved and not overwritten to 'Estimator'
            self.assertEqual(session_state.get("planreader_user"), legit_user)
            self.assertEqual(session_state.get("planreader_user")["username"], "alice_senior")
            self.assertEqual(session_state.get("planreader_user")["role"], "Senior Estimator")

    @patch("streamlit.set_page_config")
    @patch("streamlit.sidebar")
    def test_unauthenticated_path_stops_at_login_screen(self, mock_sidebar, mock_set_page_config):
        """D. Normal unauthenticated path reaches login_screen."""
        session_state = {}
        
        with patch.object(app.st, "session_state", session_state), \
             patch.object(app.st, "query_params", {}), \
             patch.object(app, "login_screen", self.login_screen_mock), \
             patch.object(app, "sidebar_workspace_selector", self.sidebar_ws_mock), \
             patch.object(app, "get_jobhub_bridge", self.get_bridge_mock), \
             patch.object(app, "init_local_db", self.init_db_mock), \
             patch.object(app, "app_css", MagicMock()):
            
            app.main()
            
            self.login_screen_mock.assert_called_once()
            self.assertIsNone(session_state.get("planreader_user"))


if __name__ == "__main__":
    unittest.main()
