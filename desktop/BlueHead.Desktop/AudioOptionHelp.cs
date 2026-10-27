using System.Windows;
using System.Windows.Input;
using System.Windows.Automation;

namespace BlueHead.Desktop;

public partial class MainWindow
{
    private HelpSection[] openHelpSections = [];
    internal string OptionHelpPlainText => string.Join("\n\n", openHelpSections.Select(section => section.Title + "\n" + section.Description));
    private void RefreshOpenHelp()
    {
        if (!AudioOptionHelpPopup.IsOpen) return;
        string topic = AudioOptionHelpPopup.Tag?.ToString() ?? "codec";
        var sections = OptionHelpContent.Sections(topic, settings.mode, settings.maximum_devices, State.Devices.Count, CodecHint.Text);
        string text = string.Join("\n\n", sections.Select(section => section.Title + "\n" + section.Description));
        if (text == OptionHelpPlainText) return;
        AudioOptionHelpTitle.Text = OptionHelpContent.Title(topic);
        openHelpSections = sections;
        AudioOptionHelpSections.ItemsSource = sections.Where(section => !section.Highlight).ToArray();
        var fact = sections.LastOrDefault(section => section.Highlight);
        AudioOptionHelpFact.Visibility = fact == null ? Visibility.Collapsed : Visibility.Visible;
        AudioOptionHelpFactTitle.Text = fact?.Title ?? ""; AudioOptionHelpFactText.Text = fact?.Description ?? "";
        AutomationProperties.SetHelpText(AudioOptionHelpSections, text);
    }
    private void AudioOptionHelpClick(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement anchor) return;
        string topic = anchor.Tag?.ToString() ?? "codec";
        bool wasOpen = AudioOptionHelpPopup.IsOpen && Equals(AudioOptionHelpPopup.Tag, topic);
        CloseAudioEditors();
        if (wasOpen) return;
        AudioOptionHelpPopup.Tag = topic; AudioOptionHelpPopup.PlacementTarget = anchor;
        AudioOptionHelpPopup.IsOpen = true; RefreshOpenHelp(); AudioOptionHelpScroll.ScrollToTop();
    }
    private void AudioOptionHelpOpened(object? sender, EventArgs e) => CloseOptionHelpButton.Focus();
    private void CloseOptionHelp()
    {
        AudioOptionHelpPopup.IsOpen = false;
        AudioOptionHelpPopup.PlacementTarget?.Focus();
    }
    private void CloseOptionHelpClick(object sender, RoutedEventArgs e) => CloseOptionHelp();
    private void OptionHelpKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Escape) { CloseOptionHelp(); e.Handled = true; }
    }
}
