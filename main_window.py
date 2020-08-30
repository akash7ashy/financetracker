import os
import logging
from functools import partial

from PySide2.QtCore import Slot, QDateTime, QDir, QLocale
from PySide2.QtGui import QIcon
from PySide2.QtWidgets import QMainWindow, QFileDialog, QMenu, QMessageBox, QLabel, QActionGroup, QAction

from UI.ui_main_window import Ui_LedgerMainWindow
from UI.ui_abort_window import Ui_AbortWindow
from CustomUI.helpers import g_tr, VLine, ManipulateDate
from CustomUI.table_view_config import TableViewConfig
from constants import TransactionType
from DB.backup_restore import MakeBackup, RestoreBackup
from DB.helpers import get_dbfilename, executeSQL
from downloader import QuoteDownloader
from ledger import Ledger
from operations import LedgerOperationsView, LedgerInitValues
from reports.reports import Reports, ReportType
from statements import StatementLoader
from reports.taxes import TaxesRus
from slips import ImportSlipDialog


#-----------------------------------------------------------------------------------------------------------------------
# This simly displays one message and OK button - to facilitate start-up error communication
class AbortWindow(QMainWindow, Ui_AbortWindow):
    def __init__(self, msg):
        QMainWindow.__init__(self, None)
        self.setupUi(self)

        self.MessageLbl.setText(msg)

#-----------------------------------------------------------------------------------------------------------------------
class MainWindow(QMainWindow, Ui_LedgerMainWindow):
    def __init__(self, db, own_path, language):
        QMainWindow.__init__(self, None)
        self.setupUi(self)

        self.db = db
        self.own_path = own_path
        self.currentLanguage = language

        self.ledger = Ledger(self.db)
        self.downloader = QuoteDownloader(self.db)
        self.downloader.download_completed.connect(self.onQuotesDownloadCompletion)
        self.taxes = TaxesRus(self.db)
        self.statements = StatementLoader(self, self.db)
        self.statements.load_completed.connect(self.onStatementLoaded)
        self.statements.load_failed.connect(self.onStatementLoadFailure)

        # Customize Status bar and logs
        self.NewLogEventLbl = QLabel(self)
        self.StatusBar.addPermanentWidget(VLine())
        self.StatusBar.addPermanentWidget(self.NewLogEventLbl)
        self.Logs.setNotificationLabel(self.NewLogEventLbl)
        self.Logs.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        self.logger = logging.getLogger()
        self.logger.addHandler(self.Logs)
        self.logger.setLevel(logging.INFO)

        # Setup reports tab
        self.ReportAccountBtn.init_db(self.db)
        self.reports = Reports(self.db, self.ReportTableView)
        self.reports.report_failure.connect(self.onReportFailure)

        # Customize UI configuration
        self.operations = LedgerOperationsView(self.OperationsTableView)
        self.ui_config = TableViewConfig(self)

        self.ui_config.configure_all()
        self.operation_details = {
            TransactionType.Action: (
                g_tr('TableViewConfig', "Income / Spending"), self.ui_config.mappers[self.ui_config.ACTIONS], 'actions',
                self.ActionDetailsTableView, 'action_details', LedgerInitValues[TransactionType.Action]),
            TransactionType.Trade: (
                g_tr('TableViewConfig', "Trade"), self.ui_config.mappers[self.ui_config.TRADES], 'trades', None, None,
                LedgerInitValues[TransactionType.Trade]),
            TransactionType.Dividend: (
                g_tr('TableViewConfig', "Dividend"), self.ui_config.mappers[self.ui_config.DIVIDENDS], 'dividends', None, None,
                LedgerInitValues[TransactionType.Dividend]),
            TransactionType.Transfer: (
                g_tr('TableViewConfig', "Transfer"), self.ui_config.mappers[self.ui_config.TRANSFERS], 'transfers_combined', None, None,
                LedgerInitValues[TransactionType.Transfer])
        }
        self.operations.setOperationsDetails(self.operation_details)
        self.operations.activateOperationView.connect(self.ShowOperationTab)
        self.operations.stateIsCommitted.connect(self.showCommitted)
        self.operations.stateIsModified.connect(self.showModified)

        # Setup balance and holdings tables
        self.ledger.setViews(self.BalancesTableView, self.HoldingsTableView)
        self.BalanceDate.setDateTime(QDateTime.currentDateTime())
        self.BalancesCurrencyCombo.init_db(self.db)   # this line will trigger onBalanceDateChange -> view updated
        self.HoldingsDate.setDateTime(QDateTime.currentDateTime())
        self.HoldingsCurrencyCombo.init_db(self.db)   # and this will trigger onHoldingsDateChange -> view updated

        # Create menu for different operations
        self.ChooseAccountBtn.init_db(self.db)
        self.NewOperationMenu = QMenu()
        for operation in self.operation_details:
            self.NewOperationMenu.addAction(self.operation_details[operation][LedgerOperationsView.OP_NAME],
                                            partial(self.operations.addNewOperation, operation))
        self.NewOperationBtn.setMenu(self.NewOperationMenu)

        self.ActionDetailsTableView.horizontalHeader().moveSection(self.ActionDetailsTableView.model().fieldIndex("note"),
                                                                   self.ActionDetailsTableView.model().fieldIndex("name"))

        self.langGroup = QActionGroup(self.menuLanguage)
        self.createLanguageMenu()
        self.langGroup.triggered.connect(self.onLanguageChanged)

        self.OperationsTableView.selectRow(0)  # TODO find a way to select last row from self.operations
        self.OnOperationsRangeChange(0)

    @Slot()
    def closeEvent(self, event):
        self.logger.removeHandler(self.Logs)    # Removing handler (but it doesn't prevent exception at exit)
        logging.raiseExceptions = False         # Silencing logging module exceptions
        self.db.close()                         # Closing database file

    def createLanguageMenu(self):
        langPath = self.own_path + "languages" + os.sep

        langDirectory = QDir(langPath)
        for language_file in langDirectory.entryList(['*.qm']):
            language_code = language_file.split('.')[0]
            language = QLocale.languageToString(QLocale(language_code).language())
            language_icon = QIcon(langPath + language_code + '.png')
            action = QAction(language_icon, language, self)
            action.setCheckable(True)
            action.setData(language_code)
            self.menuLanguage.addAction(action)
            self.langGroup.addAction(action)

    @Slot()
    def onLanguageChanged(self, action):
        language_code = action.data()
        if language_code != self.currentLanguage:
            executeSQL(self.db,
                       "UPDATE settings "
                       "SET value=(SELECT id FROM languages WHERE language = :new_language) WHERE name ='Language'",
                       [(':new_language', language_code)])
            QMessageBox().information(self, g_tr('MainWindow', "Restart required"),
                                      g_tr('MainWindow', "Language was changed to ") +
                                      QLocale.languageToString(QLocale(language_code).language()) + "\n" +
                                      g_tr('MainWindow', "You should restart application to apply changes\n"
                                           "Application will be terminated now"),
                                      QMessageBox.Ok)
            self.close()


    def Backup(self):
        backup_directory = QFileDialog.getExistingDirectory(self, g_tr('MainWindow', "Select directory to save backup"))
        if backup_directory:
            MakeBackup(get_dbfilename(self.own_path), backup_directory)

    def Restore(self):
        restore_directory = QFileDialog.getExistingDirectory(self, g_tr('MainWindow',
                                                                        "Select directory to restore from"))
        if restore_directory:
            self.db.close()
            RestoreBackup(get_dbfilename(self.own_path), restore_directory)
            QMessageBox().information(self, g_tr('MainWindow', "Data restored"),
                                      g_tr('MainWindow', "Database was loaded from the backup.\n") +
                                      g_tr('MainWindow', "You should restart application to apply changes\n"
                                           "Application will be terminated now"),
                                      QMessageBox.Ok)
            self.close()

    @Slot()
    def onBalanceDateChange(self, _new_date):
        self.ledger.setBalancesDate(self.BalanceDate.dateTime().toSecsSinceEpoch())

    @Slot()
    def onHoldingsDateChange(self, _new_date):
        self.ledger.setHoldingsDate(self.HoldingsDate.dateTime().toSecsSinceEpoch())

    @Slot()
    def OnBalanceCurrencyChange(self, _currency_index):
        self.ledger.setBalancesCurrency(self.BalancesCurrencyCombo.selected_currency(),
                                        self.BalancesCurrencyCombo.selected_currency_name())

    @Slot()
    def OnHoldingsCurrencyChange(self, _currency_index):
        self.ledger.setHoldingsCurrency(self.HoldingsCurrencyCombo.selected_currency(),
                                        self.HoldingsCurrencyCombo.selected_currency_name())

    @Slot()
    def OnBalanceInactiveChange(self, state):
        if state == 0:
            self.ledger.setActiveBalancesOnly(1)
        else:
            self.ledger.setActiveBalancesOnly(0)

    @Slot()
    def onReportRangeChange(self, range_index):
        report_ranges = {
            0: lambda: (0, 0),
            1: ManipulateDate.Last3Months,
            2: ManipulateDate.RangeYTD,
            3: ManipulateDate.RangeThisYear,
            4: ManipulateDate.RangePreviousYear
        }
        begin, end = report_ranges[range_index]()
        self.ReportFromDate.setDateTime(QDateTime.fromSecsSinceEpoch(begin))
        self.ReportToDate.setDateTime(QDateTime.fromSecsSinceEpoch(end))

    @Slot()
    def onRunReport(self):
        types = {
            0: ReportType.IncomeSpending,
            1: ReportType.ProfitLoss,
            2: ReportType.Deals
        }
        report_type = types[self.ReportTypeCombo.currentIndex()]
        begin = self.ReportFromDate.dateTime().toSecsSinceEpoch()
        end = self.ReportToDate.dateTime().toSecsSinceEpoch()
        group_dates = 1 if self.ReportGroupCheck.isChecked() else 0
        self.reports.runReport(report_type, begin, end, self.ReportAccountBtn.account_id, group_dates)

    @Slot()
    def onReportFailure(self, error_msg):
        self.StatusBar.showMessage(error_msg, timeout=30000)

    @Slot()
    def OnSearchTextChange(self):
        self.operations.setSearchText(self.SearchString.text())

    @Slot()
    def OnOperationsRangeChange(self, range_index):
        view_ranges = {
            0: ManipulateDate.startOfPreviousWeek,
            1: ManipulateDate.startOfPreviousMonth,
            2: ManipulateDate.startOfPreviousQuarter,
            3: ManipulateDate.startOfPreviousYear,
            4: lambda: 0
        }
        self.operations.setOperationsRange(view_ranges[range_index]())

    @Slot()
    def onQuotesDownloadCompletion(self):
        self.StatusBar.showMessage(g_tr('MainWindow', "Quotes download completed"), timeout=60000)
        self.ledger.updateBalancesView()
        self.ledger.updateBalancesView()

    @Slot()
    def onStatementLoaded(self):
        self.StatusBar.showMessage(g_tr('MainWindow', "Statement load completed"), timeout=60000)
        self.ledger.rebuild()

    @Slot()
    def onStatementLoadFailure(self):
        self.StatusBar.showMessage(g_tr('MainWindow', "Statement load failed"), timeout=60000)

    @Slot()
    def ShowOperationTab(self, operation_type):
        tab_list = {
            TransactionType.NA: 0,
            TransactionType.Action: 1,
            TransactionType.Transfer: 4,
            TransactionType.Trade: 2,
            TransactionType.Dividend: 3
        }
        self.OperationsTabs.setCurrentIndex(tab_list[operation_type])

    @Slot()
    def showCommitted(self):
        self.ledger.rebuild()
        self.SaveOperationBtn.setEnabled(False)
        self.RevertOperationBtn.setEnabled(False)

    @Slot()
    def showModified(self):
        self.SaveOperationBtn.setEnabled(True)
        self.RevertOperationBtn.setEnabled(True)

    @Slot()
    def importSlip(self):
        dialog = ImportSlipDialog(self, self.db)
        dialog.show()