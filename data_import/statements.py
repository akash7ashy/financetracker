import logging
import math
import re
from datetime import datetime

import pandas
from PySide2.QtCore import QObject, Signal
from PySide2.QtSql import QSqlTableModel
from PySide2.QtWidgets import QDialog, QFileDialog
from ibflex import parser, AssetClass, BuySell, CashAction, Reorg, Code
from constants import Setup, TransactionType, PredefinedAsset, PredefinedCategory, CorporateAction
from db.helpers import executeSQL, readSQL, get_country_by_code
from ui_custom.helpers import g_tr
from ui.ui_add_asset_dlg import Ui_AddAssetDialog


#-----------------------------------------------------------------------------------------------------------------------
class ReportType:
    IBKR = 'IBKR flex-query (*.xml)'
    Quik = 'Quik HTML-report (*.htm)'


#-----------------------------------------------------------------------------------------------------------------------
class IBKR:
    TaxNotePattern = "^(.*) - (..) TAX$"
    AssetType = {
        AssetClass.STOCK: PredefinedAsset.Stock,
        AssetClass.BOND: PredefinedAsset.Bond,
        AssetClass.OPTION: PredefinedAsset.Derivative,
        AssetClass.FUTURE: PredefinedAsset.Derivative
    }
    DummyExchange = "VALUE"
    SpinOffPattern = "^(.*)\(.* SPINOFF +(\d+) +FOR +(\d+) +\(.*$"
    IssueChangePattern = "^(.*)\.OLD$"
    SplitPattern = "^.* SPLIT +(\d+) +FOR +(\d+) +\(.*$"


#-----------------------------------------------------------------------------------------------------------------------
class Quik:
    ClientPattern = "^Код клиента: (.*)$"
    DateTime = 'Дата и время заключения сделки'
    TradeNumber = 'Номер сделки'
    Symbol = 'Код инструмента'
    Name = 'Краткое наименование инструмента'
    Type = 'Направление'
    Qty = 'Кол-во'
    Price = 'Цена'
    Amount = 'Объём'
    Coupon = 'НКД'
    SettleDate = 'Дата расчётов'
    Buy = 'Купля'
    Sell = 'Продажа'
    Fee = 'Комиссия Брокера'
    FeeEx = 'Суммарная комиссия ТС'    # This line is used in KIT Broker reports
    FeeEx1 = 'Комиссия за ИТС'         # Below 3 lines are used in Uralsib Borker reports
    FeeEx2 = 'Комиссия за организацию торговли'
    FeeEx3 = 'Клиринговая комиссия'
    Total = 'ИТОГО'


#-----------------------------------------------------------------------------------------------------------------------
# Strip white spaces from numbers imported form Quik html-report
def convert_amount(val):
    val = val.replace(' ', '')
    try:
        res = float(val)
    except ValueError:
        res = 0
    return res


def addNewAsset(db, symbol, name, asset_type, isin, data_source=-1):
    _ = executeSQL(db, "INSERT INTO assets(name, type_id, full_name, isin, src_id) "
                       "VALUES(:symbol, :type, :full_name, :isin, :data_src)",
                   [(":symbol", symbol), (":type", asset_type), (":full_name", name),
                    (":isin", isin), (":data_src", data_source)])
    db.commit()
    asset_id = readSQL(db, "SELECT id FROM assets WHERE name=:symbol", [(":symbol", symbol)])
    if asset_id is None:
        logging.error(g_tr('', "Failed to add new asset: "), + f"{symbol}")
    return asset_id


#-----------------------------------------------------------------------------------------------------------------------
class AddAssetDialog(QDialog, Ui_AddAssetDialog):
    def __init__(self, parent, db, symbol):
        QDialog.__init__(self)
        self.setupUi(self)
        self.db = db
        self.asset_id = None

        self.SymbolEdit.setText(symbol)

        self.type_model = QSqlTableModel(db=db)
        self.type_model.setTable('asset_types')
        self.type_model.select()
        self.TypeCombo.setModel(self.type_model)
        self.TypeCombo.setModelColumn(1)

        self.data_src_model = QSqlTableModel(db=db)
        self.data_src_model.setTable('data_sources')
        self.data_src_model.select()
        self.DataSrcCombo.setModel(self.data_src_model)
        self.DataSrcCombo.setModelColumn(1)

        # center dialog with respect to parent window
        x = parent.x() + parent.width()/2 - self.width()/2
        y = parent.y() + parent.height()/2 - self.height()/2
        self.setGeometry(x, y, self.width(), self.height())

    def accept(self):
        self.asset_id = addNewAsset(self.db, self.SymbolEdit.text(), self.NameEdit.text(),
                                    self.type_model.record(self.TypeCombo.currentIndex()).value("id"),
                                    self.isinEdit.text(),
                                    self.data_src_model.record(self.DataSrcCombo.currentIndex()).value("id"))
        super().accept()


#-----------------------------------------------------------------------------------------------------------------------
class StatementLoader(QObject):
    load_completed = Signal()
    load_failed = Signal()

    def __init__(self, parent, db):
        super().__init__()
        self.parent = parent
        self.db = db
        self.loaders = {
            ReportType.IBKR: self.loadIBFlex,
            ReportType.Quik: self.loadQuikHtml
        }
        self.ib_trade_loaders = {
            AssetClass.STOCK: self.loadIBStockTrade,
            AssetClass.OPTION: self.loadIBStockTrade,
            AssetClass.CASH: self.loadIBCurrencyTrade
        }
        self.currentIBstatement = None

    # Displays file choose dialog and loads corresponding report if user have chosen a file
    def loadReport(self):
        report_file, active_filter = \
            QFileDialog.getOpenFileName(None, g_tr('StatementLoader', "Select statement file to import"),
                                        ".", f"{ReportType.IBKR};;{ReportType.Quik}")
        if report_file:
            result = self.loaders[active_filter](report_file)
            if result:
                self.load_completed.emit()
            else:
                self.load_failed.emit()

    # Searches for account_id by account number and optional currency
    # Returns: account_id or None if no account was found
    def findAccountID(self, accountNumber, accountCurrency=''):
        if accountCurrency:
            account_id = readSQL(self.db, "SELECT a.id FROM accounts AS a "
                                          "LEFT JOIN assets AS c ON c.id=a.currency_id "
                                          "WHERE a.number=:account_number AND c.name=:currency_name",
                                 [(":account_number", accountNumber), (":currency_name", accountCurrency)])
        else:
            account_id = readSQL(self.db, "SELECT a.id FROM accounts AS a "
                                          "LEFT JOIN assets AS c ON c.id=a.currency_id "
                                          "WHERE a.number=:account_number", [(":account_number", accountNumber)])
        return account_id

    # Searches for asset_id in database and returns it.
    # If asset is not found - shows dialog for new asset creation.
    # Returns: asset_id or None if new asset creation failed
    def findAssetID(self, symbol):
        asset_id = readSQL(self.db, "SELECT id FROM assets WHERE name=:symbol", [(":symbol", symbol)])
        if asset_id is None:
            dialog = AddAssetDialog(self.parent, self.db, symbol)
            dialog.exec_()
            asset_id = dialog.asset_id
        return asset_id

    # returns bank id assigned for the account or asks for assignment if field is empty
    def getAccountBank(self, account_id):
        bank_id = readSQL(self.db, "SELECT organization_id FROM accounts WHERE id=:account_id",
                          [(":account_id", account_id)])
        if bank_id != '':
            return bank_id
        bank_id = readSQL(self.db, "SELECT id FROM agents WHERE name='Interactive Brokers'")
        if bank_id is not None:
            return bank_id
        query = executeSQL(self.db, "INSERT INTO agents (pid, name) VALUES (0, 'Interactive Brokers')")
        bank_id =query.lastInsertId()
        _ = executeSQL(self.db, "UPDATE accounts SET organization_id=:bank_id WHERE id=:account_id",
                       [(":bank_id", bank_id), (":account_id", account_id)])
        return bank_id

    def loadIBFlex(self, filename):
        try:
            report = parser.parse(filename)
        except Exception as e:
            logging.error(g_tr('StatementLoader', "Failed to parse Interactive Brokers flex-report") + f": {e}")
            return False
        for statement in report.FlexStatements:
            self.currentIBstatement = statement
            self.loadIBStatement(statement)
        return True

    def loadIBStatement(self, IBstatement):
        logging.info(g_tr('StatementLoader', "Load IB Flex-statement for account ") + f"{IBstatement.accountId} " +
                     g_tr('StatementLoader', "from ") + f"{IBstatement.fromDate}" +
                     g_tr('StatementLoader', " to ") + f"{IBstatement.toDate}")

        for asset in IBstatement.SecuritiesInfo:
            if self.storeIBAsset(asset) is None:
                logging.error(g_tr('StatementLoader', "Asset load failed: ") + f"{asset}")
                return False

        for trade in IBstatement.Trades:
            try:
                if not self.ib_trade_loaders[trade.assetCategory](trade):
                    logging.error(g_tr('StatementLoader', "Trade load failed: ") + f"{trade}")
                    return False
            except:
                logging.error(g_tr('StatementLoader', "Load of ") + f"{trade.assetCategory}" +
                              g_tr('StatementLoader', " is not implemented. Trade load failed: ") + f"{trade}")
                return False

        for tax in IBstatement.TransactionTaxes:
            if not self.loadIBTransactionTax(tax):
                logging.error(g_tr('StatementLoader', "Trade load failed: ") + f"{tax}")
                return False

        for corp_action in IBstatement.CorporateActions:
            if not self.loadIBCorpAction(corp_action):
                logging.error(g_tr('StatementLoader', "Corporate action load failed: ") + f"{corp_action}")
                return False

        # 1st loop to load all dividends separately - to allow tax match in 2nd loop
        for cash_transaction in IBstatement.CashTransactions:
            if cash_transaction.type == CashAction.DIVIDEND:
                if not self.loadIBDividend(cash_transaction):
                    logging.error(g_tr('StatementLoader', "Dividend load failed: ") + f"{cash_transaction}")
                    return False

        loadCashTransaction = {
            CashAction.WHTAX: self.loadIBWithholdingTax,
            CashAction.FEES: self.loadIBFee,
            CashAction.DEPOSITWITHDRAW: self.loadIBDepositWithdraw,
            CashAction.BROKERINTPAID: self.loadIBFee,
            CashAction.BROKERINTRCVD: self.loadIBInterest
        }
        for cash_transaction in IBstatement.CashTransactions:
            try:
                if not loadCashTransaction[cash_transaction.type](cash_transaction):
                    logging.error(g_tr('StatementLoader', "Transaction load failed: ") + f"{cash_transaction}")
                    return False
            except:
                logging.error(g_tr('StatementLoader', "Load of ") + f"{cash_transaction.type}" +
                              g_tr('StatementLoader', " is not implemented. Transaction load failed: ") +
                              f"{cash_transaction}")
                return False

        logging.info(g_tr('StatementLoader', "IB Flex-statement loaded successfully"))
        return True

    def storeIBAsset(self, IBasset):
        asset_id = readSQL(self.db, "SELECT id FROM assets WHERE name=:symbol", [(":symbol", IBasset.symbol)])
        if asset_id is not None:
            return asset_id
        try:
            asset_type = IBKR.AssetType[IBasset.assetCategory]
        except:
            logging.error(g_tr('StatementLoader', "Asset type ") + f"{IBasset.assetCategory}" +
                          g_tr('StatementLoader', " is not supported"))
            return None
        if IBasset.subCategory == "ETF":
            asset_type = PredefinedAsset.ETF
        return addNewAsset(self.db, IBasset.symbol, IBasset.description, asset_type, IBasset.isin)

    def loadIBStockTrade(self, trade):
        trade_action = {
            BuySell.BUY: self.createTrade,
            BuySell.SELL: self.createTrade,
            BuySell.CANCELBUY: self.deleteTrade,
            BuySell.CANCELSELL: self.deleteTrade
        }
        account_id = self.findAccountID(trade.accountId, trade.currency)
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") + f"{trade.accountId} ({trade.currency})")
            return False
        asset_id = self.findAssetID(trade.symbol)
        timestamp = int(trade.dateTime.timestamp())
        settlement = 0
        if trade.settleDateTarget:
            settlement = int(datetime.combine(trade.settleDateTarget, datetime.min.time()).timestamp())
        number = trade.tradeID if trade.tradeID else ""
        qty = trade.quantity * trade.multiplier
        price = trade.tradePrice
        fee = trade.ibCommission
        try:
            trade_action[trade.buySell](account_id, asset_id, timestamp, settlement, number, qty, price, fee)
            return True
        except:
            logging.error(g_tr('StatementLoader', "Trade type is not implemented: ") + f"{trade.buySell}")
            return False

    def createTrade(self, account_id, asset_id, timestamp, settlement, number, qty, price, fee, coupon=0.0):
        trade_id = readSQL(self.db,
                           "SELECT id FROM trades "
                           "WHERE timestamp=:timestamp AND asset_id = :asset "
                           "AND account_id = :account AND number = :number AND qty = :qty AND price = :price",
                           [(":timestamp", timestamp), (":asset", asset_id), (":account", account_id),
                            (":number", number), (":qty", qty), (":price", price)])
        if trade_id:
            logging.info(g_tr('StatementLoader', "Trade #") + f"{number}" +
                         g_tr('StatementLoader', " already exists in ledger. Skipped"))
            return

        _ = executeSQL(self.db,
                       "INSERT INTO trades (timestamp, settlement, number, account_id, "
                       "asset_id, qty, price, fee, coupon) "
                       "VALUES (:timestamp, :settlement, :number, :account, :asset, :qty, :price, :fee, :coupon)",
                       [(":timestamp", timestamp), (":settlement", settlement), (":number", number),
                        (":account", account_id), (":asset", asset_id), (":qty", float(qty)),
                        (":price", float(price)), (":fee", -float(fee)), (":coupon", float(coupon))])
        self.db.commit()

    def deleteTrade(self, account_id, asset_id, timestamp, _settlement, number, qty, price, _fee):
        _ = executeSQL(self.db, "DELETE FROM trades "
                                "WHERE timestamp=:timestamp AND asset_id=:asset "
                                "AND account_id=:account AND number=:number AND qty=:qty AND price=:price",
                       [(":timestamp", timestamp), (":asset", asset_id), (":account", account_id),
                        (":number", number), (":qty", -qty), (":price", price)])
        self.db.commit()

    def loadIBCurrencyTrade(self, trade):
        if trade.buySell == BuySell.BUY:
            from_idx = 1
            to_idx = 0
            to_amount = float(trade.quantity)  # positive value
            from_amount = float(trade.proceeds)  # already negative value
        elif trade.buySell == BuySell.SELL:
            from_idx = 0
            to_idx = 1
            from_amount = float(trade.quantity)  # already negative value
            to_amount = float(trade.proceeds)  # positive value
        else:
            logging.error(g_tr('StatementLoader', "Currency transaction type isn't implemented: ") + f"{trade.buySell}")
            return False
        currency = trade.symbol.split('.')
        to_account = self.findAccountID(trade.accountId, currency[to_idx])
        from_account = self.findAccountID(trade.accountId, currency[from_idx])
        fee_account = self.findAccountID(trade.accountId, trade.ibCommissionCurrency)
        if to_account is None or from_account is None or fee_account is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") + f"{trade.accountId} ({currency[to_idx]})")
            return False
        timestamp = int(trade.dateTime.timestamp())
        fee = float(trade.ibCommission)  # already negative value
        note = trade.exchange
        self.createTransfer(timestamp, from_account, from_amount, to_account, to_amount, fee_account, fee, note)
        return True

    def createTransfer(self, timestamp, f_acc_id, f_amount, t_acc_id, t_amount, fee_acc_id, fee, note):
        transfer_id = readSQL(self.db,
                              "SELECT id FROM transfers_combined "
                              "WHERE from_timestamp=:timestamp AND from_acc_id=:from_acc_id AND to_acc_id=:to_acc_id",
                              [(":timestamp", timestamp), (":from_acc_id", f_acc_id), (":to_acc_id", t_acc_id)])
        if transfer_id:
            logging.info(f"Currency exchange {f_amount}->{t_amount} already exists in ledger. Skipped")
            return
        if abs(fee) > Setup.CALC_TOLERANCE:
            _ = executeSQL(self.db,
                           "INSERT INTO transfers_combined (from_timestamp, from_acc_id, from_amount, "
                           "to_timestamp, to_acc_id, to_amount, fee_timestamp, fee_acc_id, fee_amount, note) "
                           "VALUES (:timestamp, :f_acc_id, :f_amount, :timestamp, :t_acc_id, :t_amount, "
                           ":timestamp, :fee_acc_id, :fee_amount, :note)",
                           [(":timestamp", timestamp), (":f_acc_id", f_acc_id), (":t_acc_id", t_acc_id),
                            (":f_amount", f_amount), (":t_amount", t_amount), (":fee_acc_id", fee_acc_id),
                            (":fee_amount", fee), (":note", note)])
        else:
            _ = executeSQL(self.db,
                           "INSERT INTO transfers_combined (from_timestamp, from_acc_id, from_amount, "
                           "to_timestamp, to_acc_id, to_amount, note) "
                           "VALUES (:timestamp, :f_acc_id, :f_amount, :timestamp, :t_acc_id, :t_amount, :note)",
                           [(":timestamp", timestamp), (":f_acc_id", f_acc_id), (":t_acc_id", t_acc_id),
                            (":f_amount", f_amount), (":t_amount", t_amount), (":note", note)])
        self.db.commit()

    def loadIBTransactionTax(self, IBtax):
        account_id = self.findAccountID(IBtax.accountId, IBtax.currency)
        bank_id = self.getAccountBank(account_id)
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") + f"{IBtax.accountId} ({IBtax.currency})")
            return False
        timestamp = int(datetime.combine(IBtax.date, datetime.min.time()).timestamp())
        amount = float(IBtax.taxAmount)  # value is negative already
        note = f"{IBtax.symbol} ({IBtax.description}) - {IBtax.taxDescription} (#{IBtax.tradeId})"

        id = readSQL(self.db, "SELECT id FROM all_operations WHERE type = :type "
                              "AND timestamp=:timestamp AND account_id=:account_id AND amount=:amount",
                     [(":timestamp", timestamp), (":type", TransactionType.Action),
                      (":account_id", account_id), (":amount", amount)])
        if id:
            logging.warning(g_tr('StatementLoader', "Tax transaction #") + f"{IBtax.tradeId}" +
                            g_tr('StatementLoader', " already exists"))
            return True
        query = executeSQL(self.db, "INSERT INTO actions (timestamp, account_id, peer_id) "
                                    "VALUES (:timestamp, :account_id, :bank_id)",
                           [(":timestamp", timestamp), (":account_id", account_id), (":bank_id", bank_id)])
        pid = query.lastInsertId()
        _ = executeSQL(self.db, "INSERT INTO action_details (pid, category_id, sum, note) "
                                "VALUES (:pid, :category_id, :sum, :note)",
                       [(":pid", pid), (":category_id", PredefinedCategory.Taxes), (":sum", amount), (":note", note)])
        self.db.commit()
        return True

    def createCorpAction(self, account_id, type, timestamp, number, asset_id_old, qty_old, asset_id_new, qty_new, note):
        action_id = readSQL(self.db,
                           "SELECT id FROM corp_actions "
                           "WHERE timestamp=:timestamp AND type = :type AND account_id = :account AND number = :number "
                           "AND asset_id = :asset AND asset_id_new = :asset_new",
                           [(":timestamp", timestamp), (":type", type), (":account", account_id), (":number", number),
                            (":asset", asset_id_old), (":asset_new", asset_id_new)])
        if action_id:
            logging.info(g_tr('StatementLoader', "Corp.Action #") + f"{number}" +
                         g_tr('StatementLoader', " already exists in ledger. Skipped"))
            return

        _ = executeSQL(self.db,
                       "INSERT INTO corp_actions (timestamp, number, account_id, type, "
                       "asset_id, qty, asset_id_new, qty_new, note) "
                       "VALUES (:timestamp, :number, :account, :type, :asset, :qty, :asset_new, :qty_new, :note)",
                       [(":timestamp", timestamp), (":number", number), (":account", account_id), (":type", type),
                        (":asset", asset_id_old), (":qty", float(qty_old)),
                        (":asset_new", asset_id_new), (":qty_new", float(qty_new)), (":note", note)])
        self.db.commit()

    def getPairedCorpActionRecord(self, transaction_id):
        for corp_action in self.currentIBstatement.CorporateActions:
            if corp_action.listingExchange == IBKR.DummyExchange and corp_action.transactionID==str(transaction_id):
                return corp_action
        return None

    def loadIBCorpAction(self, IBCorpAction):
        if IBCorpAction.listingExchange == IBKR.DummyExchange:   # Skip actions that we loaded as part of main action
            return True
        if IBCorpAction.code == Code.CANCEL:
            logging.warning(g_tr('StatementLoader', "*** MANUAL ACTION REQUIRED ***"))
            logging.warning(g_tr('StatementLoader', "Corporate action cancelled: ") + f"{IBCorpAction}")
            return True
        account_id = self.findAccountID(IBCorpAction.accountId, IBCorpAction.currency)
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") +
                          f"{IBCorpAction.accountId} ({IBCorpAction.currency})")
            return False
        if IBCorpAction.assetCategory != AssetClass.STOCK:
            logging.warning(g_tr('StatementLoader', "Corporate action not supported for asset class: ")
                            + f"{IBCorpAction.assetCategory}")
            return False
        if IBCorpAction.type == Reorg.MERGER:
            asset_id_new = self.findAssetID(IBCorpAction.symbol)
            timestamp = int(IBCorpAction.dateTime.timestamp())
            number = IBCorpAction.transactionID
            qty_new = IBCorpAction.quantity
            note = IBCorpAction.description
            # additional info is in previous dummy record where original symbol and quantity are present
            paired_record = self.getPairedCorpActionRecord(int(number) - 1)
            if paired_record is None:
                logging.error(g_tr('StatementLoader', "Can't find paired record for Merger corp.action"))
                return
            asset_id_old = self.findAssetID(paired_record.symbol)
            qty_old = -paired_record.quantity
            self.createCorpAction(account_id, CorporateAction.Merger, timestamp, number, asset_id_old,
                                  qty_old, asset_id_new, qty_new, note)
        elif IBCorpAction.type == Reorg.SPINOFF:
            asset_id_new = self.findAssetID(IBCorpAction.symbol)
            timestamp = int(IBCorpAction.dateTime.timestamp())
            number = IBCorpAction.transactionID
            qty_new = IBCorpAction.quantity
            note = IBCorpAction.description
            parts = re.match(IBKR.SpinOffPattern, note, re.IGNORECASE)
            if not parts:
                logging.error(g_tr('StatementLoader', "Failed to parse Spin-off data"))
                return
            asset_id_old = self.findAssetID(parts.group(1))
            mult_a = int(parts.group(2))
            mult_b = int(parts.group(3))
            qty_old = mult_b * qty_new / mult_a
            self.createCorpAction(account_id, CorporateAction.SpinOff, timestamp, number, asset_id_old,
                                  qty_old, asset_id_new, qty_new, note)
        elif IBCorpAction.type == Reorg.ISSUECHANGE:
            asset_id_new = self.findAssetID(IBCorpAction.symbol)
            timestamp = int(IBCorpAction.dateTime.timestamp())
            number = IBCorpAction.transactionID
            qty_new = IBCorpAction.quantity
            qty_old = qty_new
            note = IBCorpAction.description
            # additional info is in next dummy record where old symbol is changed to *.OLD
            paired_record = self.getPairedCorpActionRecord(int(number) + 1)
            if paired_record is None:
                logging.error(g_tr('StatementLoader', "Can't find paired record for Issue Change corp.action"))
                return
            parts = re.match(IBKR.IssueChangePattern, paired_record.symbol)
            if not parts:
                logging.error(g_tr('StatementLoader', "Failed to parse old symbol for Issue Change corp.action"))
                return
            asset_id_old = self.findAssetID(parts.group(1))
            self.createCorpAction(account_id, CorporateAction.SymbolChange, timestamp, number, asset_id_old,
                                  qty_old, asset_id_new, qty_new, note)
        elif IBCorpAction.type == Reorg.CHOICEDIVISSUE:
            asset_id = self.findAssetID(IBCorpAction.symbol)
            timestamp = int(IBCorpAction.dateTime.timestamp())
            number = IBCorpAction.transactionID
            qty_new = IBCorpAction.quantity
            note = IBCorpAction.description
            self.createCorpAction(account_id, CorporateAction.StockDividend, timestamp, number, asset_id, 0,
                                  asset_id, qty_new, note)
        elif IBCorpAction.type == Reorg.FORWARDSPLIT:
            asset_id_old = self.findAssetID(IBCorpAction.symbol)
            asset_id_new = asset_id_old
            timestamp = int(IBCorpAction.dateTime.timestamp())
            number = IBCorpAction.transactionID
            qty_old = IBCorpAction.quantity
            note = IBCorpAction.description
            parts = re.match(IBKR.SplitPattern, note, re.IGNORECASE)
            if not parts:
                logging.error(g_tr('StatementLoader', "Failed to parse corp.action Split data"))
                return
            mult_a = int(parts.group(1))
            mult_b = int(parts.group(2))
            qty_new = mult_a * qty_old / mult_b
            self.createCorpAction(account_id, CorporateAction.Split, timestamp, number, asset_id_old,
                                  qty_old, asset_id_new, qty_new, note)
        else:
            logging.error(g_tr('StatementLoader', "Corporate action type is not supported: ")
                            + f"{IBCorpAction.type}")
            return False
        return True

    def loadIBDividend(self, dividend):
        account_id = self.findAccountID(dividend.accountId, dividend.currency)
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") + f"{dividend.accountId} ({dividend.currency})")
            return False
        asset_id = self.findAssetID(dividend.symbol)
        timestamp = int(dividend.dateTime.timestamp())
        amount = float(dividend.amount)
        note = dividend.description
        self.createDividend(timestamp, account_id, asset_id, amount, note)
        return True

    def loadIBWithholdingTax(self, tax):
        account_id = self.findAccountID(tax.accountId, tax.currency)
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") + f"{tax.accountId} ({tax.currency})")
            return False
        asset_id = self.findAssetID(tax.symbol)
        timestamp = int(tax.dateTime.timestamp())
        amount = -float(tax.amount)
        note = tax.description
        self.addWithholdingTax(timestamp, account_id, asset_id, amount, note)
        return True

    def loadIBFee(self, fee):
        account_id = self.findAccountID(fee.accountId, fee.currency)
        bank_id = self.getAccountBank(account_id)
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") + f"{fee.accountId} ({fee.currency})")
            return False
        timestamp = int(fee.dateTime.timestamp())
        amount = float(fee.amount)  # value may be both positive and negative
        note = fee.description
        query = executeSQL(self.db,"INSERT INTO actions (timestamp, account_id, peer_id) "
                                   "VALUES (:timestamp, :account_id, :bank_id)",
                           [(":timestamp", timestamp), (":account_id", account_id), (":bank_id", bank_id)])
        pid = query.lastInsertId()
        _ = executeSQL(self.db, "INSERT INTO action_details (pid, category_id, sum, note) "
                                "VALUES (:pid, :category_id, :sum, :note)",
                       [(":pid", pid), (":category_id", PredefinedCategory.Fees), (":sum", amount), (":note", note)])
        self.db.commit()

    def loadIBInterest(self, interest):
        account_id = self.findAccountID(interest.accountId, interest.currency)
        bank_id = self.getAccountBank(account_id)
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account not found: ") + f"{interest.accountId} ({interest.currency})")
            return False
        timestamp = int(interest.dateTime.timestamp())
        amount = float(interest.amount)  # value may be both positive and negative
        note = interest.description
        query = executeSQL(self.db,"INSERT INTO actions (timestamp, account_id, peer_id) "
                                   "VALUES (:timestamp, :account_id, :bank_id)",
                           [(":timestamp", timestamp), (":account_id", account_id), (":bank_id", bank_id)])
        pid = query.lastInsertId()
        _ = executeSQL(self.db, "INSERT INTO action_details (pid, category_id, sum, note) "
                                "VALUES (:pid, :category_id, :sum, :note)",
                       [(":pid", pid), (":category_id", PredefinedCategory.Interest), (":sum", amount), (":note", note)])
        self.db.commit()

    # noinspection PyMethodMayBeStatic
    def loadIBDepositWithdraw(self, cash):
        logging.warning(g_tr('StatementLoader', "*** MANUAL ENTRY REQUIRED ***"))
        logging.warning(f"{cash.dateTime} {cash.description}: {cash.accountId} {cash.amount} {cash.currency}")

    def createDividend(self, timestamp, account_id, asset_id, amount, note):
        id = readSQL(self.db, "SELECT id FROM dividends WHERE timestamp=:timestamp "
                              "AND account_id=:account_id AND asset_id=:asset_id AND note=:note",
                     [(":timestamp", timestamp), (":account_id", account_id), (":asset_id", asset_id), (":note", note)])
        if id:
            logging.warning(g_tr('StatementLoader', "Dividend already exists: ") + f"{note}")
            return
        _ = executeSQL(self.db, "INSERT INTO dividends (timestamp, account_id, asset_id, sum, note) "
                                "VALUES (:timestamp, :account_id, :asset_id, :sum, :note)",
                       [(":timestamp", timestamp), (":account_id", account_id), (":asset_id", asset_id),
                        (":sum", amount), (":note", note)])
        self.db.commit()

    def addWithholdingTax(self, timestamp, account_id, asset_id, amount, note):
        parts = re.match(IBKR.TaxNotePattern, note)
        if not parts:
            logging.warning(g_tr('StatementLoader', "*** MANUAL ENTRY REQUIRED ***"))
            logging.warning(g_tr('StatementLoader', "Unhandled tax pattern found: ") + f"{note}")
            return
        dividend_note = parts.group(1) + '%'
        country_code = parts.group(2).lower()
        country_id = get_country_by_code(self.db, country_code)
        if country_id == 0:
            query = executeSQL(self.db, "INSERT INTO countries(name, code, tax_treaty) VALUES (:name, :code, 0)",
                               [(":name", "Country_" + country_code), (":code", country_code)])
            country_id = query.lastInsertId()
            logging.warning(g_tr('StatementLoader', "New dummy country added with code ") + country_code)
        try:
            dividend_id, old_tax = readSQL(self.db,
                                           "SELECT id, sum_tax FROM dividends "
                                           "WHERE timestamp=:timestamp AND account_id=:account_id "
                                           "AND asset_id=:asset_id AND note LIKE :dividend_description",
                                           [(":timestamp", timestamp), (":account_id", account_id),
                                            (":asset_id", asset_id), (":dividend_description", dividend_note)])
        except:
            logging.warning(g_tr('StatementLoader', "Dividend not found for withholding tax: ") + f"{note}")
            return
        _ = executeSQL(self.db, "UPDATE dividends SET sum_tax=:tax, tax_country_id=:country_id WHERE id=:dividend_id",
                       [(":dividend_id", dividend_id), (":tax", old_tax + amount), (":country_id", country_id)])
        self.db.commit()

    def loadQuikHtml(self, filename):
        try:
            data = pandas.read_html(filename, encoding='cp1251',
                                    converters={Quik.Qty: convert_amount, Quik.Amount: convert_amount,
                                                Quik.Price: convert_amount, Quik.Coupon: convert_amount})
        except:
            logging.error(g_tr('StatementLoader', "Can't read statement file"))
            return False

        report_info = data[0]
        deals_info = data[1]
        parts = re.match(Quik.ClientPattern, report_info[0][2])
        if parts:
            account_id = self.findAccountID(parts.group(1))
        else:
            logging.error(g_tr('StatementLoader', "Can't get account number from the statement."))
            return False
        if account_id is None:
            logging.error(g_tr('StatementLoader', "Account with number ") + f"{parts.group(1)}" +
                          g_tr('StatementLoader', " not found. Import cancelled."))
            return False

        for index, row in deals_info.iterrows():
            if row[Quik.Type] == Quik.Buy:
                qty = int(row[Quik.Qty])
            elif row[Quik.Type] == Quik.Sell:
                qty = -int(row[Quik.Qty])
            elif row[Quik.Type][:len(Quik.Total)] == Quik.Total:
                break   # End of statement reached
            else:
                logging.warning(g_tr('StatementLoader', "Unknown operation type ") + f"'{row[Quik.Type]}'")
                continue
            asset_id = self.findAssetID(row[Quik.Symbol])
            if asset_id is None:
                logging.warning(g_tr('StatementLoader', "Unknown asset ") + f"'{row[Quik.Symbol]}'")
                continue
            timestamp = int(datetime.strptime(row[Quik.DateTime], "%d.%m.%Y %H:%M:%S").timestamp())
            settlement = int(datetime.strptime(row[Quik.SettleDate], "%d.%m.%Y").timestamp())
            number = row[Quik.TradeNumber]
            price = row[Quik.Price]
            amount = row[Quik.Amount]
            lot_size = math.pow(10, round(math.log10(amount / (price * abs(qty)))))
            qty = qty * lot_size
            fee = float(row[Quik.Fee])
            if Quik.FeeEx in row:  # Broker dependent fee import
                fee = fee + float(row[Quik.FeeEx])
            else:
                fee = fee + float(row[Quik.FeeEx1]) + float(row[Quik.FeeEx2]) + float(row[Quik.FeeEx3])
            coupon = float(row[Quik.Coupon])
            self.createTrade(account_id, asset_id, timestamp, settlement, number, qty, price, -fee, coupon)
        return True