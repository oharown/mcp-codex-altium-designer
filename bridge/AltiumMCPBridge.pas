{ Minimal DelphiScript bridge for Codex <-> MCP <-> Altium Designer.

  How it works:
  - StartMCPBridge runs inside Altium Designer.
  - It polls shared\request.json.
  - The Python MCP server writes requests there.
  - This script executes a small allowlist of read-only commands and writes
    shared\response.json.

  StartMCPBridge runs a short bounded polling window. This avoids locking
  Altium Designer's UI indefinitely when DelphiScript runs on the UI thread.
  Re-run StartMCPBridge when Codex is waiting for a request, or stop it early
  by calling the altium_stop_bridge MCP tool / creating shared\bridge.stop.
}

const
    { Replace C:\path\to\mcp-codex-altium-designer with your local clone path. }
    BridgeDir = 'C:\path\to\mcp-codex-altium-designer\shared';
    RequestFile = 'C:\path\to\mcp-codex-altium-designer\shared\request.json';
    ResponseFile = 'C:\path\to\mcp-codex-altium-designer\shared\response.json';
    HeartbeatFile = 'C:\path\to\mcp-codex-altium-designer\shared\heartbeat.json';
    StopFile = 'C:\path\to\mcp-codex-altium-designer\shared\bridge.stop';
    MaxPollTicks = 40;


function JsonEscape(S : String) : String;
var
    I : Integer;
    C : String;
begin
    Result := '';
    for I := 1 to Length(S) do
    begin
        C := Copy(S, I, 1);
        if C = '\' then
            Result := Result + '\\'
        else if C = '"' then
            Result := Result + '\"'
        else if C = #13 then
            Result := Result + '\r'
        else if C = #10 then
            Result := Result + '\n'
        else
            Result := Result + C;
    end;
end;


function JsonString(Name : String; Value : String) : String;
begin
    Result := '"' + JsonEscape(Name) + '":"' + JsonEscape(Value) + '"';
end;


function JsonBool(Name : String; Value : Boolean) : String;
begin
    if Value then
        Result := '"' + JsonEscape(Name) + '":true'
    else
        Result := '"' + JsonEscape(Name) + '":false';
end;


function JsonInt(Name : String; Value : Integer) : String;
begin
    Result := '"' + JsonEscape(Name) + '":' + IntToStr(Value);
end;


function JsonOk(Id : String; Payload : String) : String;
begin
    Result := '{"id":"' + JsonEscape(Id) + '","ok":true,"result":' + Payload + '}';
end;


function JsonError(Id : String; MessageText : String) : String;
begin
    Result := '{"id":"' + JsonEscape(Id) + '","ok":false,"error":"' + JsonEscape(MessageText) + '"}';
end;


function ReadTextFile(FileName : String) : String;
var
    Lines : TStringList;
begin
    Result := '';
    Lines := TStringList.Create;
    try
        Lines.LoadFromFile(FileName);
        Result := Lines.Text;
    finally
        Lines.Free;
    end;
end;


procedure WriteTextFile(FileName : String; Text : String);
var
    Lines : TStringList;
begin
    Lines := TStringList.Create;
    try
        Lines.Text := Text;
        Lines.SaveToFile(FileName);
    finally
        Lines.Free;
    end;
end;


function ExtractJsonString(Json : String; Key : String) : String;
var
    Search : String;
    P : Integer;
    I : Integer;
    C : String;
    Escaped : Boolean;
begin
    Result := '';
    Search := '"' + Key + '"';
    P := Pos(Search, Json);
    if P = 0 then Exit;

    I := P + Length(Search);
    while (I <= Length(Json)) and (Copy(Json, I, 1) <> ':') do Inc(I);
    if I > Length(Json) then Exit;
    Inc(I);

    while (I <= Length(Json)) and (Copy(Json, I, 1) <> '"') do Inc(I);
    if I > Length(Json) then Exit;
    Inc(I);

    Escaped := False;
    while I <= Length(Json) do
    begin
        C := Copy(Json, I, 1);
        if Escaped then
        begin
            Result := Result + C;
            Escaped := False;
        end
        else if C = '\' then
            Escaped := True
        else if C = '"' then
            Exit
        else
            Result := Result + C;
        Inc(I);
    end;
end;


function TabField(Line : String; FieldIndex : Integer) : String;
var
    I : Integer;
    StartPos : Integer;
    CurrentIndex : Integer;
    C : String;
begin
    Result := '';
    StartPos := 1;
    CurrentIndex := 0;

    for I := 1 to Length(Line) + 1 do
    begin
        if I > Length(Line) then
            C := #9
        else
            C := Copy(Line, I, 1);

        if C = #9 then
        begin
            if CurrentIndex = FieldIndex then
            begin
                Result := Copy(Line, StartPos, I - StartPos);
                Exit;
            end;

            Inc(CurrentIndex);
            StartPos := I + 1;
        end;
    end;
end;


function DocumentJson(Doc : Variant) : String;
var
    KindText : String;
    PathText : String;
    FileText : String;
    Loaded : Boolean;
begin
    if Doc = Nil then
    begin
        Result := '{"has_document":false}';
        Exit;
    end;

    KindText := '';
    PathText := '';
    FileText := '';
    Loaded := False;

    try
        KindText := Doc.DM_DocumentKind;
    except
        KindText := '';
    end;

    try
        PathText := Doc.DM_FullPath;
    except
        PathText := '';
    end;

    try
        FileText := Doc.DM_FileName;
    except
        FileText := '';
    end;

    try
        Loaded := Doc.DM_DocumentIsLoaded;
    except
        Loaded := False;
    end;

    Result := '{"has_document":true,' +
        JsonString('kind', KindText) + ',' +
        JsonString('full_path', PathText) + ',' +
        JsonString('file_name', FileText) + ',' +
        JsonBool('loaded', Loaded) + '}';
end;


function ActiveDocumentJson : String;
var
    WS : Variant;
    Doc : Variant;
begin
    Result := '{"has_active_document":false}';

    WS := GetWorkspace;
    if WS = Nil then
    begin
        Result := '{"has_active_document":false,"error":"GetWorkspace returned nil"}';
        Exit;
    end;

    Doc := WS.DM_FocusedDocument;
    if Doc = Nil then Exit;

    Result := DocumentJson(Doc);
end;


function ProjectJson(AProject : Variant) : String;
var
    ProjectPath : String;
    ProjectFile : String;
    LogicalCount : Integer;
    PhysicalCount : Integer;
    I : Integer;
    Doc : Variant;
    LogicalDocs : String;
    PhysicalDocs : String;
begin
    if AProject = Nil then
    begin
        Result := '{"has_project":false}';
        Exit;
    end;

    ProjectPath := '';
    ProjectFile := '';
    LogicalCount := 0;
    PhysicalCount := 0;

    try
        ProjectPath := AProject.DM_ProjectFullPath;
    except
        ProjectPath := '';
    end;

    try
        ProjectFile := AProject.DM_ProjectFileName;
    except
        ProjectFile := '';
    end;

    try
        LogicalCount := AProject.DM_LogicalDocumentCount;
    except
        LogicalCount := 0;
    end;

    try
        PhysicalCount := AProject.DM_PhysicalDocumentCount;
    except
        PhysicalCount := 0;
    end;

    LogicalDocs := '[';
    for I := 0 to LogicalCount - 1 do
    begin
        if I > 0 then LogicalDocs := LogicalDocs + ',';
        try
            Doc := AProject.DM_LogicalDocuments(I);
            LogicalDocs := LogicalDocs + DocumentJson(Doc);
        except
            LogicalDocs := LogicalDocs + '{"error":"Unable to read logical document"}';
        end;
    end;
    LogicalDocs := LogicalDocs + ']';

    PhysicalDocs := '[';
    for I := 0 to PhysicalCount - 1 do
    begin
        if I > 0 then PhysicalDocs := PhysicalDocs + ',';
        try
            Doc := AProject.DM_PhysicalDocuments(I);
            PhysicalDocs := PhysicalDocs + DocumentJson(Doc);
        except
            PhysicalDocs := PhysicalDocs + '{"error":"Unable to read physical document"}';
        end;
    end;
    PhysicalDocs := PhysicalDocs + ']';

    Result := '{"has_project":true,' +
        JsonString('project_full_path', ProjectPath) + ',' +
        JsonString('project_file_name', ProjectFile) + ',' +
        '"logical_document_count":' + IntToStr(LogicalCount) + ',' +
        '"logical_documents":' + LogicalDocs + ',' +
        '"physical_document_count":' + IntToStr(PhysicalCount) + ',' +
        '"physical_documents":' + PhysicalDocs + '}';
end;


function WorkspaceDocumentsJson : String;
var
    WS : Variant;
    FocusedDoc : Variant;
    FocusedProject : Variant;
    AProject : Variant;
    ProjectCount : Integer;
    I : Integer;
    ProjectsJson : String;
begin
    Result := '{"has_workspace":false}';

    WS := GetWorkspace;
    if WS = Nil then
    begin
        Result := '{"has_workspace":false,"error":"GetWorkspace returned nil"}';
        Exit;
    end;

    ProjectCount := 0;
    try
        ProjectCount := WS.DM_ProjectCount;
    except
        ProjectCount := 0;
    end;

    FocusedDoc := Nil;
    try
        FocusedDoc := WS.DM_FocusedDocument;
    except
        FocusedDoc := Nil;
    end;

    FocusedProject := Nil;
    try
        FocusedProject := WS.DM_FocusedProject;
    except
        FocusedProject := Nil;
    end;

    ProjectsJson := '[';
    for I := 0 to ProjectCount - 1 do
    begin
        if I > 0 then ProjectsJson := ProjectsJson + ',';
        try
            AProject := WS.DM_Projects(I);
            ProjectsJson := ProjectsJson + ProjectJson(AProject);
        except
            ProjectsJson := ProjectsJson + '{"error":"Unable to read project"}';
        end;
    end;
    ProjectsJson := ProjectsJson + ']';

    Result := '{"has_workspace":true,' +
        '"project_count":' + IntToStr(ProjectCount) + ',' +
        '"focused_document":' + DocumentJson(FocusedDoc) + ',' +
        '"focused_project":' + ProjectJson(FocusedProject) + ',' +
        '"projects":' + ProjectsJson + '}';
end;


function FirstWorkspaceDocumentPathByKind(TargetKind : String) : String;
var
    WS : Variant;
    AProject : Variant;
    Doc : Variant;
    ProjectCount : Integer;
    LogicalCount : Integer;
    I : Integer;
    J : Integer;
    KindText : String;
    PathText : String;
begin
    Result := '';

    WS := GetWorkspace;
    if WS = Nil then Exit;

    ProjectCount := 0;
    try
        ProjectCount := WS.DM_ProjectCount;
    except
        ProjectCount := 0;
    end;

    for I := 0 to ProjectCount - 1 do
    begin
        try
            AProject := WS.DM_Projects(I);
        except
            AProject := Nil;
        end;

        if AProject <> Nil then
        begin
            LogicalCount := 0;
            try
                LogicalCount := AProject.DM_LogicalDocumentCount;
            except
                LogicalCount := 0;
            end;

            for J := 0 to LogicalCount - 1 do
            begin
                try
                    Doc := AProject.DM_LogicalDocuments(J);
                except
                    Doc := Nil;
                end;

                if Doc <> Nil then
                begin
                    KindText := '';
                    PathText := '';

                    try
                        KindText := Doc.DM_DocumentKind;
                    except
                        KindText := '';
                    end;

                    try
                        PathText := Doc.DM_FullPath;
                    except
                        PathText := '';
                    end;

                    if (KindText = TargetKind) and (PathText <> '') then
                    begin
                        Result := PathText;
                        Exit;
                    end;
                end;
            end;
        end;
    end;
end;


function ActivateFirstWorkspaceDocumentByKind(TargetKind : String) : Boolean;
var
    PathText : String;
    ServerDocument : Variant;
begin
    Result := False;
    PathText := FirstWorkspaceDocumentPathByKind(TargetKind);
    if PathText = '' then Exit;

    try
        Client.StartServer(TargetKind);
    except
    end;

    try
        ServerDocument := Client.OpenDocument(TargetKind, PathText);
        if ServerDocument <> Nil then
        begin
            Client.ShowDocument(ServerDocument);
            Result := True;
        end;
    except
        Result := False;
    end;
end;


function PcbComponentsJson : String;
var
    Board : Variant;
    Iterator : Variant;
    Component : Variant;
    First : Boolean;
    Designator : String;
    CommentText : String;
    FootprintText : String;
    Count : Integer;
begin
    Result := '{"document_type":"pcb","count":0,"components":[]}';

    Board := PCBServer.GetCurrentPCBBoard;
    if Board = Nil then
    begin
        if ActivateFirstWorkspaceDocumentByKind('PCB') then
        begin
            Sleep(500);
            Board := PCBServer.GetCurrentPCBBoard;
        end;
    end;

    if Board = Nil then
    begin
        Result := '{"document_type":"pcb","error":"No current PCB board","count":0,"components":[]}';
        Exit;
    end;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eComponentObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    First := True;
    Count := 0;
    Result := '{"document_type":"pcb","components":[';

    Component := Iterator.FirstPCBObject;
    while Component <> Nil do
    begin
        Designator := '';
        CommentText := '';
        FootprintText := '';

        try
            Designator := Component.Name.Text;
        except
            Designator := '';
        end;

        try
            CommentText := Component.Comment.Text;
        except
            CommentText := '';
        end;

        try
            FootprintText := Component.Pattern;
        except
            FootprintText := '';
        end;

        if FootprintText = '' then
        begin
            try
                FootprintText := Component.SourceFootprint;
            except
                FootprintText := '';
            end;
        end;

        if FootprintText = '' then
        begin
            try
                FootprintText := Component.Footprint;
            except
                FootprintText := '';
            end;
        end;

        if not First then Result := Result + ',';
        Result := Result + '{' +
            JsonString('designator', Designator) + ',' +
            JsonString('comment', CommentText) + ',' +
            JsonString('footprint', FootprintText) + '}';

        First := False;
        Inc(Count);
        Component := Iterator.NextPCBObject;
    end;

    Board.BoardIterator_Destroy(Iterator);
    Result := Result + '],"count":' + IntToStr(Count) + '}';
end;


function PcbNetsJson : String;
var
    Board : Variant;
    Iterator : Variant;
    NetObj : Variant;
    First : Boolean;
    NetName : String;
    PinCount : Integer;
    ViaCount : Integer;
    RoutedLength : Integer;
    ConnectsVisible : Boolean;
    ConnectivelyInvalid : Boolean;
    Count : Integer;
begin
    Result := '{"document_type":"pcb_nets","count":0,"nets":[]}';

    Board := PCBServer.GetCurrentPCBBoard;
    if Board = Nil then
    begin
        if ActivateFirstWorkspaceDocumentByKind('PCB') then
        begin
            Sleep(500);
            Board := PCBServer.GetCurrentPCBBoard;
        end;
    end;

    if Board = Nil then
    begin
        Result := '{"document_type":"pcb_nets","error":"No current PCB board","count":0,"nets":[]}';
        Exit;
    end;

    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eNetObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);

    First := True;
    Count := 0;
    Result := '{"document_type":"pcb_nets","nets":[';

    NetObj := Iterator.FirstPCBObject;
    while NetObj <> Nil do
    begin
        NetName := '';
        PinCount := 0;
        ViaCount := 0;
        RoutedLength := 0;
        ConnectsVisible := False;
        ConnectivelyInvalid := False;

        try
            NetName := NetObj.Name;
        except
            NetName := '';
        end;

        try
            PinCount := NetObj.PinCount;
        except
            PinCount := 0;
        end;

        try
            ViaCount := NetObj.ViaCount;
        except
            ViaCount := 0;
        end;

        try
            RoutedLength := NetObj.RoutedLength;
        except
            RoutedLength := 0;
        end;

        try
            ConnectsVisible := NetObj.ConnectsVisible;
        except
            ConnectsVisible := False;
        end;

        try
            ConnectivelyInvalid := NetObj.ConnectivelyInvalid;
        except
            ConnectivelyInvalid := False;
        end;

        if not First then Result := Result + ',';
        Result := Result + '{' +
            JsonString('name', NetName) + ',' +
            JsonInt('pin_count', PinCount) + ',' +
            JsonInt('via_count', ViaCount) + ',' +
            JsonInt('routed_length', RoutedLength) + ',' +
            JsonBool('connects_visible', ConnectsVisible) + ',' +
            JsonBool('connectively_invalid', ConnectivelyInvalid) + '}';

        First := False;
        Inc(Count);
        NetObj := Iterator.NextPCBObject;
    end;

    Board.BoardIterator_Destroy(Iterator);
    Result := Result + '],"count":' + IntToStr(Count) + '}';
end;


function SchComponentParametersJson(Component : Variant) : String;
var
    Iterator : Variant;
    ParamObj : Variant;
    First : Boolean;
    ParamName : String;
    ParamValue : String;
begin
    Result := '[]';

    try
        Iterator := Component.SchIterator_Create;
    except
        Iterator := Nil;
    end;

    if Iterator = Nil then Exit;

    try
        Iterator.AddFilter_ObjectSet(MkSet(eParameter));
    except
        Component.SchIterator_Destroy(Iterator);
        Exit;
    end;

    First := True;
    Result := '[';

    ParamObj := Iterator.FirstSchObject;
    while ParamObj <> Nil do
    begin
        ParamName := '';
        ParamValue := '';

        try
            ParamName := ParamObj.Name;
        except
            ParamName := '';
        end;

        if ParamName = '' then
        begin
            try
                ParamName := ParamObj.Name.Text;
            except
                ParamName := '';
            end;
        end;

        try
            ParamValue := ParamObj.Text;
        except
            ParamValue := '';
        end;

        if ParamName <> '' then
        begin
            if not First then Result := Result + ',';
            Result := Result + '{' +
                JsonString('name', ParamName) + ',' +
                JsonString('value', ParamValue) + '}';
            First := False;
        end;

        ParamObj := Iterator.NextSchObject;
    end;

    Component.SchIterator_Destroy(Iterator);
    Result := Result + ']';
end;


function SchComponentsJson : String;
var
    Doc : Variant;
    Iterator : Variant;
    Component : Variant;
    First : Boolean;
    Designator : String;
    CommentText : String;
    ParametersText : String;
    Count : Integer;
begin
    Result := '{"document_type":"schematic","count":0,"components":[]}';

    Doc := SchServer.GetCurrentSchDocument;
    if Doc = Nil then
    begin
        if ActivateFirstWorkspaceDocumentByKind('SCH') then
        begin
            Sleep(500);
            Doc := SchServer.GetCurrentSchDocument;
        end;
    end;

    if Doc = Nil then
    begin
        Result := '{"document_type":"schematic","error":"No current schematic document","count":0,"components":[]}';
        Exit;
    end;

    Iterator := Doc.SchIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eSchComponent));

    First := True;
    Count := 0;
    Result := '{"document_type":"schematic","components":[';

    Component := Iterator.FirstSchObject;
    while Component <> Nil do
    begin
        Designator := '';
        CommentText := '';
        ParametersText := '[]';

        try
            Designator := Component.Designator.Text;
        except
            Designator := '';
        end;

        try
            CommentText := Component.Comment.Text;
        except
            CommentText := '';
        end;

        ParametersText := SchComponentParametersJson(Component);

        if not First then Result := Result + ',';
        Result := Result + '{' +
            JsonString('designator', Designator) + ',' +
            JsonString('comment', CommentText) + ',' +
            '"parameters":' + ParametersText + '}';

        First := False;
        Inc(Count);
        Component := Iterator.NextSchObject;
    end;

    Doc.SchIterator_Destroy(Iterator);
    Result := Result + '],"count":' + IntToStr(Count) + '}';
end;


function ApplySchDesignatorUpdates(UpdatesFile : String) : String;
var
    Doc : Variant;
    Iterator : Variant;
    Component : Variant;
    Lines : TStringList;
    I : Integer;
    OldDesignator : String;
    NewDesignator : String;
    ExpectedComment : String;
    Designator : String;
    CommentText : String;
    Processed : String;
    ChangedJson : String;
    MissingJson : String;
    FirstChanged : Boolean;
    FirstMissing : Boolean;
    ChangedCount : Integer;
    MissingCount : Integer;
begin
    Result := '{"changed_count":0,"missing_count":0,"changed":[],"missing":[]}';

    if UpdatesFile = '' then
    begin
        Result := '{"error":"updates_file is required"}';
        Exit;
    end;

    if not FileExists(UpdatesFile) then
    begin
        Result := '{"error":"updates_file does not exist"}';
        Exit;
    end;

    Doc := SchServer.GetCurrentSchDocument;
    if Doc = Nil then
    begin
        if ActivateFirstWorkspaceDocumentByKind('SCH') then
        begin
            Sleep(500);
            Doc := SchServer.GetCurrentSchDocument;
        end;
    end;

    if Doc = Nil then
    begin
        Result := '{"error":"No current schematic document","changed_count":0,"missing_count":0,"changed":[],"missing":[]}';
        Exit;
    end;

    Lines := TStringList.Create;
    try
        Lines.LoadFromFile(UpdatesFile);

        Iterator := Doc.SchIterator_Create;
        Iterator.AddFilter_ObjectSet(MkSet(eSchComponent));

        Processed := '|';
        ChangedJson := '[';
        MissingJson := '[';
        FirstChanged := True;
        FirstMissing := True;
        ChangedCount := 0;
        MissingCount := 0;

        Component := Iterator.FirstSchObject;
        while Component <> Nil do
        begin
            Designator := '';
            CommentText := '';

            try
                Designator := Component.Designator.Text;
            except
                Designator := '';
            end;

            try
                CommentText := Component.Comment.Text;
            except
                CommentText := '';
            end;

            for I := 0 to Lines.Count - 1 do
            begin
                if (Trim(Lines.Strings[I]) <> '') and (Pos('|' + IntToStr(I) + '|', Processed) = 0) then
                begin
                    OldDesignator := TabField(Lines.Strings[I], 0);
                    NewDesignator := TabField(Lines.Strings[I], 1);
                    ExpectedComment := TabField(Lines.Strings[I], 2);

                    if (Designator = OldDesignator) and ((ExpectedComment = '') or (CommentText = ExpectedComment)) then
                    begin
                        try
                            Component.Designator.Text := NewDesignator;
                            if not FirstChanged then ChangedJson := ChangedJson + ',';
                            ChangedJson := ChangedJson + '{' +
                                JsonString('old_designator', OldDesignator) + ',' +
                                JsonString('new_designator', NewDesignator) + ',' +
                                JsonString('comment', CommentText) + '}';
                            FirstChanged := False;
                            Inc(ChangedCount);
                        except
                        end;

                        Processed := Processed + IntToStr(I) + '|';
                    end;
                end;
            end;

            Component := Iterator.NextSchObject;
        end;

        Doc.SchIterator_Destroy(Iterator);

        for I := 0 to Lines.Count - 1 do
        begin
            if (Trim(Lines.Strings[I]) <> '') and (Pos('|' + IntToStr(I) + '|', Processed) = 0) then
            begin
                OldDesignator := TabField(Lines.Strings[I], 0);
                NewDesignator := TabField(Lines.Strings[I], 1);
                ExpectedComment := TabField(Lines.Strings[I], 2);
                if not FirstMissing then MissingJson := MissingJson + ',';
                MissingJson := MissingJson + '{' +
                    JsonString('old_designator', OldDesignator) + ',' +
                    JsonString('new_designator', NewDesignator) + ',' +
                    JsonString('comment', ExpectedComment) + '}';
                FirstMissing := False;
                Inc(MissingCount);
            end;
        end;

        ChangedJson := ChangedJson + ']';
        MissingJson := MissingJson + ']';

        Result := '{' +
            JsonInt('changed_count', ChangedCount) + ',' +
            JsonInt('missing_count', MissingCount) + ',' +
            '"changed":' + ChangedJson + ',' +
            '"missing":' + MissingJson + '}';
    finally
        Lines.Free;
    end;
end;


function ApplySchParameterUpdates(UpdatesFile : String) : String;
var
    Doc : Variant;
    ComponentIterator : Variant;
    ParamIterator : Variant;
    Component : Variant;
    ParamObj : Variant;
    Lines : TStringList;
    I : Integer;
    TargetDesignator : String;
    TargetParamName : String;
    NewValue : String;
    Designator : String;
    ParamName : String;
    Processed : String;
    UpdatedJson : String;
    MissingJson : String;
    FirstUpdated : Boolean;
    FirstMissing : Boolean;
    UpdatedCount : Integer;
    MissingCount : Integer;
begin
    Result := '{"updated_count":0,"missing_count":0,"updated":[],"missing":[]}';

    if UpdatesFile = '' then
    begin
        Result := '{"error":"updates_file is required"}';
        Exit;
    end;

    if not FileExists(UpdatesFile) then
    begin
        Result := '{"error":"updates_file does not exist"}';
        Exit;
    end;

    Doc := SchServer.GetCurrentSchDocument;
    if Doc = Nil then
    begin
        if ActivateFirstWorkspaceDocumentByKind('SCH') then
        begin
            Sleep(500);
            Doc := SchServer.GetCurrentSchDocument;
        end;
    end;

    if Doc = Nil then
    begin
        Result := '{"error":"No current schematic document","updated_count":0,"missing_count":0,"updated":[],"missing":[]}';
        Exit;
    end;

    Lines := TStringList.Create;
    try
        Lines.LoadFromFile(UpdatesFile);

        ComponentIterator := Doc.SchIterator_Create;
        ComponentIterator.AddFilter_ObjectSet(MkSet(eSchComponent));

        Processed := '|';
        UpdatedJson := '[';
        MissingJson := '[';
        FirstUpdated := True;
        FirstMissing := True;
        UpdatedCount := 0;
        MissingCount := 0;

        Component := ComponentIterator.FirstSchObject;
        while Component <> Nil do
        begin
            Designator := '';
            try
                Designator := Component.Designator.Text;
            except
                Designator := '';
            end;

            for I := 0 to Lines.Count - 1 do
            begin
                if (Trim(Lines.Strings[I]) <> '') and (Pos('|' + IntToStr(I) + '|', Processed) = 0) then
                begin
                    TargetDesignator := TabField(Lines.Strings[I], 0);
                    TargetParamName := TabField(Lines.Strings[I], 1);
                    NewValue := TabField(Lines.Strings[I], 2);

                    if Designator = TargetDesignator then
                    begin
                        try
                            ParamIterator := Component.SchIterator_Create;
                            ParamIterator.AddFilter_ObjectSet(MkSet(eParameter));

                            ParamObj := ParamIterator.FirstSchObject;
                            while ParamObj <> Nil do
                            begin
                                ParamName := '';
                                try
                                    ParamName := ParamObj.Name;
                                except
                                    ParamName := '';
                                end;

                                if ParamName = TargetParamName then
                                begin
                                    try
                                        ParamObj.Text := NewValue;
                                        if not FirstUpdated then UpdatedJson := UpdatedJson + ',';
                                        UpdatedJson := UpdatedJson + '{' +
                                            JsonString('designator', TargetDesignator) + ',' +
                                            JsonString('parameter', TargetParamName) + ',' +
                                            JsonString('value', NewValue) + '}';
                                        FirstUpdated := False;
                                        Inc(UpdatedCount);
                                    except
                                    end;

                                    Processed := Processed + IntToStr(I) + '|';
                                    ParamObj := Nil;
                                end
                                else
                                    ParamObj := ParamIterator.NextSchObject;
                            end;

                            Component.SchIterator_Destroy(ParamIterator);
                        except
                        end;
                    end;
                end;
            end;

            Component := ComponentIterator.NextSchObject;
        end;

        Doc.SchIterator_Destroy(ComponentIterator);

        for I := 0 to Lines.Count - 1 do
        begin
            if (Trim(Lines.Strings[I]) <> '') and (Pos('|' + IntToStr(I) + '|', Processed) = 0) then
            begin
                TargetDesignator := TabField(Lines.Strings[I], 0);
                TargetParamName := TabField(Lines.Strings[I], 1);
                NewValue := TabField(Lines.Strings[I], 2);
                if not FirstMissing then MissingJson := MissingJson + ',';
                MissingJson := MissingJson + '{' +
                    JsonString('designator', TargetDesignator) + ',' +
                    JsonString('parameter', TargetParamName) + ',' +
                    JsonString('value', NewValue) + '}';
                FirstMissing := False;
                Inc(MissingCount);
            end;
        end;

        UpdatedJson := UpdatedJson + ']';
        MissingJson := MissingJson + ']';

        Result := '{' +
            JsonInt('updated_count', UpdatedCount) + ',' +
            JsonInt('missing_count', MissingCount) + ',' +
            '"updated":' + UpdatedJson + ',' +
            '"missing":' + MissingJson + '}';
    finally
        Lines.Free;
    end;
end;


function ExecuteCommand(Command : String) : String;
begin
    if Command = 'ping' then
        Result := '{"pong":true,"bridge":"AltiumMCPBridge","message":"Altium bridge is running"}'
    else if Command = 'get_active_document' then
        Result := ActiveDocumentJson
    else if Command = 'list_workspace_documents' then
        Result := WorkspaceDocumentsJson
    else if Command = 'list_pcb_components' then
        Result := PcbComponentsJson
    else if Command = 'list_pcb_nets' then
        Result := PcbNetsJson
    else if Command = 'list_sch_components' then
        Result := SchComponentsJson
    else if Command = 'apply_sch_designator_updates' then
        Result := ApplySchDesignatorUpdates(ExtractJsonString(ReadTextFile(RequestFile), 'updates_file'))
    else if Command = 'apply_sch_parameter_updates' then
        Result := ApplySchParameterUpdates(ExtractJsonString(ReadTextFile(RequestFile), 'updates_file'))
    else
        Result := '';
end;


procedure WriteHeartbeat;
begin
    WriteTextFile(HeartbeatFile, '{"bridge":"AltiumMCPBridge","status":"running"}');
end;


procedure StartMCPBridge;
var
    RequestText : String;
    Id : String;
    Command : String;
    Payload : String;
    Response : String;
    TickCount : Integer;
begin
    if FileExists(StopFile) then DeleteFile(StopFile);

    TickCount := 0;
    while (not FileExists(StopFile)) and (TickCount < MaxPollTicks) do
    begin
        Inc(TickCount);
        try
            WriteHeartbeat;

            if FileExists(RequestFile) then
            begin
                RequestText := ReadTextFile(RequestFile);
                Id := ExtractJsonString(RequestText, 'id');
                Command := ExtractJsonString(RequestText, 'command');

                if Id <> '' then
                begin
                    Payload := ExecuteCommand(Command);
                    if Payload = '' then
                        Response := JsonError(Id, 'Unsupported command: ' + Command)
                    else
                        Response := JsonOk(Id, Payload);

                    WriteTextFile(ResponseFile, Response);
                    DeleteFile(RequestFile);
                end;
            end;
        except
            if Id = '' then Id := 'unknown';
            WriteTextFile(ResponseFile, JsonError(Id, 'Unhandled DelphiScript bridge exception'));
        end;

        Sleep(250);
    end;

    DeleteFile(StopFile);
    WriteTextFile(HeartbeatFile, '{"bridge":"AltiumMCPBridge","status":"stopped"}');
end;
