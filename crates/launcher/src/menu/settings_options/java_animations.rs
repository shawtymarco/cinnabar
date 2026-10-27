//! Shared dependencies and help for the optional Java animation controls.
use super::{SettingId, layout::SettingPanel};

/// Explains how to activate an otherwise discoverable combat setting.
pub const REQUIRED_STYLE: &str = "Select Java 1.7 animations to change this option.";

/// Derives the dependency from panel ownership so new combat controls inherit it.
pub fn requires_java_animations(id: SettingId) -> bool {
    id.definition()
        .section
        .iter()
        .any(|(panel, _)| *panel == SettingPanel::VideoJavaCombat)
}

/// Explains the independent visual and movement effects in settings and search.
pub fn description(id: SettingId) -> Option<&'static str> {
    Some(match id {
        SettingId::Animations => "Choose the player and held-item animation style.",
        SettingId::ItemBlockingAnimation => {
            "Show the raised sword animation in first and third person. Visual only."
        }
        SettingId::BlockingSlowdown => {
            "Slow forward and sideways movement while holding Use with a sword."
        }
        SettingId::KeepSprintWhileBlocking => {
            "Allow sprinting while holding Use with a sword. Turn off to stop sprinting."
        }
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn java_combat_rows_have_help_and_share_their_animation_dependency() {
        for option in SettingPanel::VideoJavaCombat.options() {
            assert!(description(option.id).is_some());
            assert!(requires_java_animations(option.id));
        }
        assert!(!requires_java_animations(SettingId::Animations));
    }
}
