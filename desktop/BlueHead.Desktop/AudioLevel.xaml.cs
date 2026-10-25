using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;

namespace BlueHead.Desktop;

public partial class AudioLevel : UserControl
{
    private double lastNonZeroValue = 100;
    private static readonly DependencyPropertyKey IsSilentPropertyKey = DependencyProperty.RegisterReadOnly(nameof(IsSilent), typeof(bool), typeof(AudioLevel), new PropertyMetadata(false));
    public static readonly DependencyProperty IsSilentProperty = IsSilentPropertyKey.DependencyProperty;
    public bool IsSilent => (bool)GetValue(IsSilentProperty);
    public static readonly DependencyProperty ValueProperty = DependencyProperty.Register(nameof(Value), typeof(double), typeof(AudioLevel),
        new FrameworkPropertyMetadata(100d, FrameworkPropertyMetadataOptions.BindsTwoWayByDefault, OnValueChanged, (_, value) => double.IsFinite((double)value) ? Math.Clamp((double)value, 0, 200) : 100d));
    public static readonly DependencyProperty LabelProperty = DependencyProperty.Register(nameof(Label), typeof(string), typeof(AudioLevel), new PropertyMetadata("音量"));
    public static readonly DependencyProperty SecondaryProperty = DependencyProperty.Register(nameof(Secondary), typeof(string), typeof(AudioLevel), new PropertyMetadata(""));
    public static readonly DependencyProperty HintProperty = DependencyProperty.Register(nameof(Hint), typeof(string), typeof(AudioLevel), new PropertyMetadata(""));
    public static readonly DependencyProperty IconProperty = DependencyProperty.Register(nameof(Icon), typeof(string), typeof(AudioLevel), new PropertyMetadata("\uE767"));
    public static readonly DependencyProperty CompactLabelProperty = DependencyProperty.Register(nameof(CompactLabel), typeof(string), typeof(AudioLevel), new PropertyMetadata("音量"));
    public string CompactLabel { get => (string)GetValue(CompactLabelProperty); set => SetValue(CompactLabelProperty, value); }
    public static readonly DependencyProperty CompactProperty = DependencyProperty.Register(nameof(Compact), typeof(bool), typeof(AudioLevel), new PropertyMetadata(false));
    public static readonly DependencyProperty CanAdjustProperty = DependencyProperty.Register(nameof(CanAdjust), typeof(bool), typeof(AudioLevel), new PropertyMetadata(true));
    public static readonly DependencyProperty IsOpenProperty = DependencyProperty.Register(nameof(IsOpen), typeof(bool), typeof(AudioLevel), new PropertyMetadata(false));
    public static readonly DependencyProperty EditorContentProperty = DependencyProperty.Register(nameof(EditorContent), typeof(object), typeof(AudioLevel));
    public static readonly DependencyProperty FooterContentProperty = DependencyProperty.Register(nameof(FooterContent), typeof(object), typeof(AudioLevel));
    public double Value { get => (double)GetValue(ValueProperty); set => SetValue(ValueProperty, value); }
    public double Maximum => 200;
    public string Label { get => (string)GetValue(LabelProperty); set => SetValue(LabelProperty, value); }
    public string Secondary { get => (string)GetValue(SecondaryProperty); set => SetValue(SecondaryProperty, value); }
    public string Hint { get => (string)GetValue(HintProperty); set => SetValue(HintProperty, value); }
    public string Icon { get => (string)GetValue(IconProperty); set => SetValue(IconProperty, value); }
    public bool Compact { get => (bool)GetValue(CompactProperty); set => SetValue(CompactProperty, value); }
    public bool CanAdjust { get => (bool)GetValue(CanAdjustProperty); set => SetValue(CanAdjustProperty, value); }
    public bool IsOpen { get => (bool)GetValue(IsOpenProperty); set => SetValue(IsOpenProperty, value); }
    public object EditorContent { get => GetValue(EditorContentProperty); set => SetValue(EditorContentProperty, value); }
    public object FooterContent { get => GetValue(FooterContentProperty); set => SetValue(FooterContentProperty, value); }
    public event RoutedPropertyChangedEventHandler<double>? ValueChanged;
    private static void OnValueChanged(DependencyObject sender, DependencyPropertyChangedEventArgs e)
    {
        var control = (AudioLevel)sender;
        var value = (double)e.NewValue;
        if (value > 0) control.lastNonZeroValue = value;
        control.SetValue(IsSilentPropertyKey, value == 0);
        control.ValueChanged?.Invoke(control, new((double)e.OldValue, (double)e.NewValue));
    }
    public AudioLevel()
    {
        InitializeComponent();
        IsVisibleChanged += (_, _) => { if (!IsVisible) SetCurrentValue(IsOpenProperty, false); };
        IsEnabledChanged += (_, _) => { if (!IsEnabled) SetCurrentValue(IsOpenProperty, false); };
        Unloaded += (_, _) => SetCurrentValue(IsOpenProperty, false);
    }
    private void SilenceClick(object sender, RoutedEventArgs e) => ToggleSilence();
    internal void ToggleSilence() => SetCurrentValue(ValueProperty, Value > 0 ? 0d : lastNonZeroValue);
    private void ResetClick(object sender, RoutedEventArgs e) => SetCurrentValue(ValueProperty, 100d);
    private void FlyoutOpened(object? sender, EventArgs e)
    {
        // Popups have a separate HWND. Only one editor should own focus/capture at a time.
        if (Window.GetWindow(this) is MainWindow owner) owner.CloseAudioEditors(this);
        Panel.MoveFocus(new TraversalRequest(FocusNavigationDirection.First));
    }
    private void FlyoutClosed(object? sender, EventArgs e) => SetCurrentValue(IsOpenProperty, false);
    private void OpenerMouseDown(object sender, MouseButtonEventArgs e)
    {
        if (!IsOpen) return;
        SetCurrentValue(IsOpenProperty, false); e.Handled = true; Opener.Focus();
    }
    private void PanelKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Escape) return;
        SetCurrentValue(IsOpenProperty, false); Opener.Focus(); e.Handled = true;
    }
    internal FrameworkElement EditorPanel => Panel;
    internal Slider EditorSlider => LevelSlider;
    internal void ResetLevel() => ResetClick(this, new());
}
