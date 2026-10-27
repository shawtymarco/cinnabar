//! Java-only settings remain discoverable without offering inactive controls.
use super::*;
use crate::review_tests::paint;
use launcher::menu::settings_options::SettingId;
use std::{collections::HashMap, sync::Arc};

/// Paints settings or search through the production controls and returns their actions.
fn actions(java: bool, query: &str) -> Vec<MenuAction> {
    let mut view = crate::test_support::settings_view("video_forced_index");
    Arc::make_mut(&mut view.settings_options).set(SettingId::Animations.index(), i32::from(!java));
    view.settings_query = query.into();
    let (_, hits, _) = paint(HashMap::new(), |canvas| {
        super::draw(
            canvas,
            &view,
            [1280.0, 20000.0],
            &|_| None,
            None,
            &mut Default::default(),
        )
        .unwrap();
    });
    hits.into_iter().map(|(action, _)| action).collect()
}

#[test]
fn java_blocking_controls_are_actionable_only_with_java_animations() {
    for java in [false, true] {
        let actions = actions(java, "");
        for id in [
            SettingId::ItemBlockingAnimation,
            SettingId::BlockingSlowdown,
            SettingId::KeepSprintWhileBlocking,
        ] {
            assert_eq!(actions.iter().any(|action| matches!(action, MenuAction::SettingsOption(index, _) if usize::from(*index) == id.index())), java, "{id:?}");
        }
    }
}

#[test]
fn java_blocking_search_exposes_the_animation_selector_without_inactive_toggles() {
    let found = actions(false, "sprint blocking");
    assert!(found.iter().any(|action| matches!(action, MenuAction::SettingsOption(index, _) if usize::from(*index) == SettingId::Animations.index())), "search must let the player enable the required animation style");
    assert!(!found.iter().any(|action| matches!(action, MenuAction::SettingsOption(index, _) if usize::from(*index) == SettingId::KeepSprintWhileBlocking.index())));
}

#[test]
fn java_blocking_search_keeps_the_version_and_legacy_pose_terms_in_both_styles() {
    for java in [false, true] {
        for query in ["java 1.7 blocking", "item blocking animation"] {
            let found = actions(java, query);
            assert!(found.iter().any(|action| matches!(action, MenuAction::SettingsOption(index, _) if usize::from(*index) == SettingId::Animations.index())), "{query}");
        }
    }
}
