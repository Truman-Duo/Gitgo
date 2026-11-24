using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class SetupResult
{
    public readonly int ExitCode;
    public readonly string Output, Errors, Launcher;

    public SetupResult(int exitCode, string output, string errors, string launcher)
    {
        ExitCode = exitCode;
        Output = output;
        Errors = errors;
        Launcher = launcher;
    }

    public string DesktopState()
    {
        Dictionary<string, object> receipt;
        try { receipt = new JavaScriptSerializer().DeserializeObject(Output) as Dictionary<string, object>; }
        catch (ArgumentException) { return null; }
        object value;
        if (receipt != null && receipt.TryGetValue("native_entry", out value))
        {
            var entry = value as Dictionary<string, object>;
            if (entry != null && entry.TryGetValue("desktop_shortcut", out value))
            {
                var shortcut = value as Dictionary<string, object>;
                if (shortcut != null && shortcut.TryGetValue("state", out value))
                    return value as string;
            }
        }
        return null;
    }
}

internal sealed class SetupWizard : Form
{
    private readonly Panel content = new Panel { Dock = DockStyle.Fill };
    private readonly Label heading = new Label { AutoSize = true };
    private readonly Button primary = new Button { AutoSize = true };
    private readonly Button secondary = new Button { AutoSize = true };
    private readonly TextBox directory = new TextBox { Dock = DockStyle.Fill };
    private readonly CheckBox consent = new CheckBox { AutoSize = true };
    private readonly CheckBox desktop = new CheckBox { AutoSize = true };
    private readonly CheckBox launch = new CheckBox { AutoSize = true };
    private readonly Label message = new Label { Dock = DockStyle.Fill, AutoEllipsis = true };
    private readonly ProgressBar progress = new ProgressBar { Dock = DockStyle.Top, Style = ProgressBarStyle.Marquee };
    private readonly TableLayoutPanel options;
    private readonly Func<string, string, Action<string>, SetupResult> install;
    private readonly Action<string> open;
    private SetupResult result;
    private bool busy, failed;
    public int ExitCode { get; private set; }

    private static string Local(string chinese, string english)
    {
        return CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "zh" ? chinese : english;
    }

    private static TableLayoutPanel Grid(int columns)
    {
        return new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = columns, Padding = new Padding(0) };
    }

    private static Label LabelText(string text)
    {
        return new Label { Text = text, AutoSize = true, Margin = new Padding(0, 10, 0, 6) };
    }

    public SetupWizard(string defaultRoot, string license,
        Func<string, string, Action<string>, SetupResult> install, Action<string> open)
    {
        this.install = install;
        this.open = open;
        Text = Local("Gitgo 安装", "Gitgo Setup");
        Font = SystemFonts.MessageBoxFont;
        Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        AutoScaleMode = AutoScaleMode.Font;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(650, 488);
        MinimumSize = new Size(550, 440);
        MaximizeBox = false;

        var layout = Grid(1);
        layout.Padding = new Padding(22, 18, 22, 14);
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        heading.Font = new Font(Font.FontFamily, 16, FontStyle.Bold);
        heading.Margin = new Padding(0, 0, 0, 8);
        var actions = new FlowLayoutPanel { AutoSize = true, FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false, Dock = DockStyle.Fill, Padding = new Padding(0, 10, 0, 0) };
        primary.MinimumSize = secondary.MinimumSize = new Size(92, 30);
        actions.Controls.Add(primary);
        actions.Controls.Add(secondary);
        layout.Controls.Add(heading, 0, 0);
        layout.Controls.Add(content, 0, 1);
        layout.Controls.Add(actions, 0, 2);
        Controls.Add(layout);
        AcceptButton = primary;
        CancelButton = secondary;

        options = Grid(1);
        for (int index = 0; index < 6; index++)
            options.RowStyles.Add(new RowStyle(index == 1 ? SizeType.Percent : SizeType.AutoSize, index == 1 ? 100 : 0));
        var terms = new TextBox { Text = license, ReadOnly = true, Multiline = true,
            ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, BackColor = SystemColors.Window };
        consent.Text = Local("我已阅读并同意 Apache 2.0 许可条款", "I have read and agree to the Apache 2.0 license terms");
        consent.Margin = new Padding(0, 8, 0, 0);
        directory.Text = defaultRoot;
        var path = Grid(2);
        path.AutoSize = true;
        path.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        path.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        var browse = new Button { AutoSize = true, Text = Local("浏览…", "Browse…") };
        browse.Click += (sender, args) => Browse();
        path.Controls.Add(directory, 0, 0);
        path.Controls.Add(browse, 1, 0);
        desktop.Text = Local("创建桌面快捷方式", "Create a desktop shortcut");
        desktop.Margin = new Padding(0, 8, 0, 0);
        options.Controls.Add(LabelText(Local("许可协议", "License agreement")), 0, 0);
        options.Controls.Add(terms, 0, 1);
        options.Controls.Add(consent, 0, 2);
        options.Controls.Add(LabelText(Local("安装目录", "Installation folder")), 0, 3);
        options.Controls.Add(path, 0, 4);
        options.Controls.Add(desktop, 0, 5);

        consent.CheckedChanged += (sender, args) => UpdateInstallButton();
        directory.TextChanged += (sender, args) => UpdateInstallButton();
        primary.Click += (sender, args) => OnPrimary();
        secondary.Click += (sender, args) => Close();
        FormClosing += (sender, args) => { if (busy) args.Cancel = true; };
        ShowOptions();
    }

    private void Browse()
    {
        using (var picker = new FolderBrowserDialog { Description = Local("选择 Gitgo 安装目录", "Choose the Gitgo installation folder") })
        {
            if (Directory.Exists(directory.Text)) picker.SelectedPath = directory.Text;
            if (picker.ShowDialog(this) == DialogResult.OK) directory.Text = picker.SelectedPath;
        }
    }

    private void ShowOptions()
    {
        failed = false;
        heading.Text = Local("安装 Gitgo", "Install Gitgo");
        secondary.Text = Local("取消", "Cancel");
        primary.Text = Local("安装", "Install");
        content.Controls.Clear();
        content.Controls.Add(options);
        UpdateInstallButton();
    }

    private void UpdateInstallButton()
    {
        primary.Enabled = consent.Checked && !String.IsNullOrWhiteSpace(directory.Text);
    }

    private void ShowStatus(Control detail)
    {
        var panel = Grid(1);
        panel.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        panel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        panel.Controls.Add(message, 0, 0);
        if (detail != null) panel.Controls.Add(detail, 0, 1);
        content.Controls.Clear();
        content.Controls.Add(panel);
    }

    private async void OnPrimary()
    {
        if (busy) return;
        if (failed) { ShowOptions(); return; }
        if (result != null && result.ExitCode == 0)
        {
            if (launch.Checked)
            {
                try { open(result.Launcher); }
                catch (Exception error) { MessageBox.Show(this, error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error); return; }
            }
            Close();
            return;
        }
        if (!consent.Checked) return;
        string root;
        try
        {
            var selected = directory.Text.Trim();
            if (!Path.IsPathRooted(selected) || selected.Length < 3 || selected[1] != ':' || (selected[2] != '\\' && selected[2] != '/'))
                throw new ArgumentException();
            root = Path.GetFullPath(selected);
        }
        catch (Exception)
        {
            MessageBox.Show(this, Local("请选择本机磁盘上的绝对路径。", "Choose a full path on a local drive."),
                Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        var createDesktop = desktop.Checked;
        var desktopFolder = createDesktop ? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory) : null;
        busy = true;
        primary.Enabled = secondary.Enabled = false;
        heading.Text = Local("正在安装 Gitgo", "Installing Gitgo");
        message.Text = Local("正在校验安装包…", "Verifying the installation package…");
        ShowStatus(progress);
        try
        {
            result = await Task.Run(() => install(root, desktopFolder, phase => BeginInvoke((Action)(() =>
                message.Text = phase == "extract" ? Local("正在校验并解包…", "Verifying and extracting…")
                    : Local("正在安装程序并注册快捷方式…", "Installing the program and registering shortcuts…")))));
            if (result.ExitCode != 0)
            {
                var code = Regex.Match(result.Errors, @"\b(?:UPDATE|NATIVE_ENTRY)_[A-Z0-9_]+\b(?:: [^\r\n]{1,220})?");
                var detail = code.Success ? code.Value : Local("退出码 ", "Exit code ") + result.ExitCode;
                ShowFailure(detail);
                return;
            }
            var shortcut = createDesktop ? result.DesktopState() : null;
            ExitCode = 0;
            heading.Text = Local("安装完成", "Installation complete");
            message.Text = Local("安装位置\r\n", "Installed in\r\n") + root
                + Local("\r\n\r\n可从开始菜单或 Windows Terminal 启动 Gitgo。", "\r\n\r\nOpen Gitgo from Start or Windows Terminal.");
            if (createDesktop && shortcut != "ready")
                message.Text += shortcut == "conflict" ? Local("\r\n桌面已有 Gitgo 快捷方式，原快捷方式已保留。", "\r\nThe desktop already has a Gitgo shortcut. It was preserved.")
                    : Local("\r\n桌面快捷方式未能创建。", "\r\nThe desktop shortcut could not be created.");
            launch.Text = Local("启动 Gitgo", "Launch Gitgo");
            launch.Dock = DockStyle.Top;
            primary.Text = Local("完成", "Finish");
            secondary.Visible = false;
            ShowStatus(launch);
        }
        catch (Exception error) { ShowFailure(error.Message); }
        finally
        {
            busy = false;
            primary.Enabled = secondary.Enabled = true;
        }
    }

    private void ShowFailure(string detail)
    {
        failed = true;
        ExitCode = 1;
        heading.Text = Local("安装未完成", "Installation did not complete");
        message.Text = Local("安装器报告了以下错误：\r\n\r\n", "Setup reported the following error:\r\n\r\n") + detail;
        primary.Text = Local("返回", "Back");
        secondary.Text = Local("关闭", "Close");
        ShowStatus(null);
    }
}
